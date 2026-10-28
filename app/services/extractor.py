"""Screenshot -> structured ReferencePackage."""
from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import ValidationError

from ..errors import ExtractionError
from ..models import Component, ReferencePackage
from .imageprep import Slice, make_slices
from .llm import LLM

log = logging.getLogger(__name__)

EXTRACT_SYSTEM = """You are a meticulous travel-product data extractor. You read screenshots of a tour package web page
(top to bottom, in several overlapping parts) and output ONE JSON object. Rules:
- Use ONLY what is visible. Never invent or assume. Use null / [] when something is not shown.
- Ignore repeated availability/date rows except to report the overall date range in availability_range and the month in travel_month.
- 'components' must list EVERY distinct thing the buyer gets, one short normalised phrase each, so a competitor page can be
  checked item by item. Include: accommodation (star class and nights), breakfast/other meals that are stand-alone, ONE single item for
  all transfers (never one item per leg or per sightseeing stop),
  each named tour / attraction / activity / show / cruise / safari / ticket, guide, visa, insurance if included.
- A meal that belongs to an activity is part of THAT activity, not a separate item
  (write "Desert safari with BBQ dinner", "Dhow cruise with dinner" - never a separate "BBQ dinner" line).
- If one line says "A and B" or "A + B", output A and B as two separate items. Never list the same thing twice.
- Multi-city trips: one accommodation component PER CITY with its nights (e.g. "Hotel in Beijing (4 nights)", "Hotel in Shanghai (3 nights)");
  list every city in 'cities' and use the whole country/route as 'destination'. 'nights'/'days' are the totals for the whole package.
- Generic wording is fine ("Transportation between destinations", "All entrance fees for attractions", "Daily breakfast, lunch and dinner");
  keep it as written instead of inventing specifics. Named attractions that appear only in the itinerary text (not in the included list)
  are still components if the itinerary clearly includes them.
- Never list anything under 'Not Included'.
- category must be one of: hotel, meal, transfer, tour, attraction, activity, other.
- match_rule: ONE short sentence saying what a competitor page must show to count as the same service
  (e.g. "Desert safari in 4x4 with BBQ dinner; shared or private both acceptable").
- transfers: 'SIC' only if the page explicitly says shared / sharing basis / SIC / seat-in-coach; 'private' only if it says private.
  Do not guess from the vehicle type.
- price_amount is the headline 'from' price as a plain number; price_currency an ISO-4217 code (map $ to USD, Rs to the likely local code only if the page makes it clear, otherwise USD).
- Return JSON only, no commentary."""

EXTRACT_PROMPT = """Extract the package into this exact JSON shape:
{
 "title": str, "destination": str, "country": str|null, "cities": [str],
 "nights": int|null, "days": int|null, "hotel_star": int|null, "hotel_names": [str],
 "price_amount": number|null, "price_currency": "USD", "price_basis": "per person"|"per couple"|"per group"|null,
 "operator": str|null, "transfers": "SIC"|"private"|null, "flights_included": bool|null, "visa_included": bool|null,
 "meals_summary": str|null, "group_size": int|null, "travel_month": str|null, "availability_range": str|null,
 "cancellation_policy": str|null,
 "components": [{"name": str, "category": str, "required": true, "match_rule": str}],
 "exclusions": [str], "itinerary_summary": str|null, "extraction_notes": str|null
}
In extraction_notes, mention anything ambiguous you saw (for example a star rating that is unclear or cut off)."""


def _num(v: Any) -> Any:
    if isinstance(v, str):
        m = re.search(r"-?\d[\d,]*\.?\d*", v)
        return float(m.group().replace(",", "")) if m else None
    return v


def _int(v: Any) -> Any:
    v = _num(v)
    return int(v) if isinstance(v, (int, float)) else None


def merge_transfers(comps: list[Component]) -> list[Component]:
    """Models like to list every transfer leg. Competitors never list them that way, so keep ONE transfers component."""
    tr = [c for c in comps if c.category == "transfer"]
    if len(tr) <= 1:
        return comps
    first = comps.index(tr[0])
    merged = Component(name="Transfers (airport pick-up/drop and local transfers)", category="transfer", required=True,
                       match_rule="The page states that airport and/or local transfers are included (shared or private both acceptable).")
    out = [c for c in comps if c.category != "transfer"]
    out.insert(min(first, len(out)), merged)
    return out


def coerce_reference(data: dict[str, Any]) -> ReferencePackage:
    d = dict(data)
    for k in ("nights", "days", "hotel_star", "group_size"):
        d[k] = _int(d.get(k))
    d["price_amount"] = _num(d.get("price_amount"))
    if d.get("hotel_star") is not None and not 1 <= d["hotel_star"] <= 5:
        d["hotel_star"] = None
    comps = []
    for c in d.get("components") or []:
        if isinstance(c, str):
            comps.append(Component(name=c))
        elif isinstance(c, dict) and c.get("name"):
            comps.append(Component(name=str(c["name"]), category=str(c.get("category") or "other"),
                                   required=bool(c.get("required", True)),
                                   match_rule=(str(c["match_rule"]).strip()[:300] if c.get("match_rule") else None)))
    comps = merge_transfers(comps)
    seen, uniq = set(), []
    for c in comps:
        key = c.name.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    d["components"] = uniq
    for k in ("cities", "hotel_names", "exclusions"):
        v = d.get(k)
        d[k] = [str(x) for x in v] if isinstance(v, list) else []
    for k in ("title", "destination"):
        d[k] = str(d.get(k) or "").strip()
    if d.get("transfers") not in ("SIC", "private", None):
        t = str(d["transfers"]).lower()
        d["transfers"] = "SIC" if ("shar" in t or "sic" in t) else ("private" if "priv" in t else None)
    if not isinstance(d.get("flights_included"), bool):
        d["flights_included"] = False if d.get("flights_included") is None else None
    if not isinstance(d.get("visa_included"), bool):
        d["visa_included"] = None
    try:
        return ReferencePackage(**{k: v for k, v in d.items() if k in ReferencePackage.model_fields})
    except ValidationError as e:
        raise ExtractionError("The extracted package data was malformed.", details=str(e))


def extract_reference(llm: LLM, raw_image: bytes) -> ReferencePackage:
    slices: list[Slice] = make_slices(raw_image)
    log.info("Extracting package from %d image slice(s)", len(slices))
    data = llm.vision_json(slices, EXTRACT_SYSTEM, EXTRACT_PROMPT)
    ref = coerce_reference(data)
    if not (ref.destination or ref.title) or not ref.components:
        raise ExtractionError(
            "Could not find package details in this screenshot. Make sure it is a full-page capture of a package "
            "page showing its inclusions or itinerary.",
            details=str(data)[:500],
        )
    return ref
