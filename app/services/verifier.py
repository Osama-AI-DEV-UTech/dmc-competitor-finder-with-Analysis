"""Fetch a candidate page, extract offers with the LLM, then DISTRUST the LLM: check evidence and price against the page text."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..errors import AppError
from ..models import Offer, ReferencePackage
from .fetcher import Fetcher, FetchResult
from .llm import LLM, SearchHit
from .searcher import domain_of

log = logging.getLogger(__name__)

VERIFY_SYSTEM = """You extract competitor travel-package offers from the text of ONE web page and compare each to a reference package.
Rules:
- Report ONLY a real whole package: several days, with accommodation, sold as one product with ONE price for the whole thing.
  offer_kind must be one of: multi_day_package | single_activity | activity_combo | hotel_only | flight_only | other.
  A desert safari, a city tour, a cruise, a ticket, a day trip or a hotel room alone is NOT a package: report it with the right offer_kind
  (or skip it) - never as multi_day_package.
- price_scope: 'entire_package' only if the displayed price is for the whole multi-day package; 'single_item' if it is for one activity/ticket/room;
  otherwise 'unclear'.
- Copy prices exactly as displayed. NEVER convert currencies and never invent a price. If no price is shown use null.
- nights / days / hotel_star: give them ONLY if the page states them for THAT offer; otherwise null. Do not infer.
- 'seller' = the company that sells/operates the package (the marketplace name only if no operator is named).
- At most 6 offers per page; if the page lists many packages choose the ones closest to the reference.
- For EVERY reference component return a status: 'yes' only if the page explicitly says THIS offer includes it AND you can quote the words;
  'no' only if explicitly excluded; otherwise 'not_stated'. Apply the component's match_rule strictly.
- evidence = a short VERBATIM quote (max 25 words) copied from the page text. Empty string if none.
- extras = things this offer INCLUDES that are NOT among the reference components (extra tours, tickets, meals, transfers, nights,
  upgrades, free services...). One short name each, with a verbatim quote. Skip anything that is just a reworded reference component,
  and skip taxes, price boilerplate and generic marketing. At most 10.
- Return JSON only."""

RISK_PHRASES = (
    "under moderation", "may not be available", "no longer available", "sold out", "expired", "offer has ended",
    "page not found", "this listing has been removed",
)

SELLER_TYPES = {"DMC", "Tour operator", "OTA", "Marketplace", "Retailer", "Other"}


def _norm_words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def evidence_in_text(evidence: str, page_words_joined: str) -> bool:
    """True if the quoted evidence really occurs on the page (a 6-word window is enough for long quotes)."""
    w = _norm_words(evidence)
    if not w:
        return False
    if len(w) <= 6:
        return " " + " ".join(w) + " " in page_words_joined
    return any(" " + " ".join(w[i:i + 6]) + " " in page_words_joined for i in range(len(w) - 5))


def price_in_text(price: float | None, text: str) -> bool:
    """Does the number appear on the page (tolerating 1,234 / 1.234 / Indian 12,34,567 grouping)?"""
    if price is None:
        return False
    flat = re.sub(r"(?<=\d),(?=\d)", "", text)
    flat = re.sub(r"(?<=\d)\s(?=\d{3}\b)", "", flat)
    candidates = {f"{price:.2f}", f"{price:.1f}", f"{price:g}"}
    if float(price).is_integer():
        candidates.add(str(int(price)))
    # a decimal form like "437.00" on the page also proves the integer 437
    for c in candidates:
        if re.search(rf"(?<![\d.]){re.escape(c)}(?!\d|\.\d)", flat):
            return True
    return False


def _f(v: Any) -> Any:
    if isinstance(v, str):
        m = re.search(r"-?\d[\d,]*\.?\d*", v)
        return float(m.group().replace(",", "")) if m else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _i(v: Any) -> Any:
    x = _f(v)
    return int(x) if x is not None else None


def _b(v: Any) -> Any:
    return v if isinstance(v, bool) else None


def build_prompt(ref: ReferencePackage, url: str, text: str) -> str:
    comps = [(c.name, c.match_rule) for c in ref.components if c.required]
    refsum = {
        "destination": ref.destination, "nights": ref.nights, "days": ref.days, "hotel_star": ref.hotel_star,
        "transfers": ref.transfers, "flights_included": ref.flights_included,
    }
    return (
        f"REFERENCE PACKAGE: {refsum}\nREFERENCE COMPONENTS (return one entry for each, use these exact names):\n"
        + "\n".join(f"- {n}" + (f"  [rule: {r}]" if r else "") for n, r in comps)
        + f"\n\nPAGE URL: {url}\nPAGE TEXT:\n\"\"\"\n{text}\n\"\"\"\n\n"
        "Return JSON: {\"offers\": [{\"seller\": str, \"seller_type\": \"DMC\"|\"Tour operator\"|\"OTA\"|\"Marketplace\"|\"Retailer\"|\"Other\", "
        "\"package_name\": str, \"offer_kind\": str, \"price_scope\": \"entire_package\"|\"single_item\"|\"unclear\", "
        "\"hotel\": str|null, \"hotel_star\": int|null, \"nights\": int|null, \"days\": int|null, "
        "\"destination\": str|null, \"price_from\": number|null, \"price_to\": number|null, \"currency\": \"ISO code\", "
        "\"price_basis\": \"per person\"|\"per couple\"|\"per group\"|\"total\"|\"unknown\", \"price_pax\": int|null, "
        "\"flights_included\": bool|null, \"transfers\": \"SIC\"|\"private\"|\"mixed\"|\"none\"|\"not_stated\", \"visa_included\": bool|null, "
        "\"availability_dates\": str|null, \"components\": [{\"name\": str, \"status\": \"yes\"|\"no\"|\"not_stated\", \"evidence\": str}], "
        "\"extras\": [{\"name\": str, \"evidence\": str}], \"notes\": str}]}. If the page has no qualifying offer return {\"offers\": []}."
    )


STOP = {"the", "and", "with", "for", "of", "to", "in", "at", "a", "an", "on", "by", "per", "from", "included", "include", "tour", "visit", "trip"}


def _toks(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2 and t not in STOP}


def overlaps_reference(name: str, ref: ReferencePackage) -> bool:
    """True when an 'extra' is really one of the reference components in other words."""
    a = _toks(name)
    if not a:
        return True
    for c in ref.components:
        b = _toks(c.name)
        if b and len(a & b) / min(len(a), len(b)) >= 0.7:
            return True
    return False


def clean_extras(raw, ref: ReferencePackage, page_words: str) -> tuple[list[str], dict[str, str]]:
    names: list[str] = []
    ev: dict[str, str] = {}
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        name = " ".join(str(e.get("name") or "").split())[:120]
        quote = str(e.get("evidence") or "").strip()
        if not name or name.lower() in {n.lower() for n in names}:
            continue
        if not evidence_in_text(quote, page_words) or overlaps_reference(name, ref):
            continue  # unquoted or duplicate-of-yours: not trusted as an extra
        names.append(name)
        ev[name] = quote[:300]
        if len(names) >= 12:
            break
    return names, ev


# ---- deterministic grounding of the facts the LLM tends to guess
def numbers_before(word_pattern: str, text: str) -> set[int]:
    return {int(m.group(1)) for m in re.finditer(rf"(?<![\d.])(\d{{1,2}})\s*[-\u2013 ]?\s*(?:{word_pattern})", text, re.I)}


def stated_nights(text: str) -> set[int]:
    return numbers_before(r"nights?\b|n\b(?=\s*/|\s*\d)", text)


def stated_days(text: str) -> set[int]:
    return numbers_before(r"days?\b|d\b(?=\s*/|\s*\W|$)", text)


def stated_stars(text: str) -> set[int]:
    found = numbers_before(r"stars?\b|\*|\u2605|\u2b50", text)
    return {x for x in found if 1 <= x <= 5}


def grounded(value: Optional[int], present: set[int], label: str, flags: list[str]) -> Optional[int]:
    if value is None:
        return None
    if value in present:
        return value
    flags.append(f"{label} {value} not found on the page (ignored)")
    return None


def lead_offer(hit: SearchHit, reason: str) -> Offer:
    dom = domain_of(hit.url)
    return Offer(seller=dom, listed_on=dom, package_name=hit.title or "(page could not be read)", url=hit.url,
                 verification="snippet", flags=[f"Could not read page: {reason}", "Needs manual check"],
                 notes="Search found this page but its content could not be verified automatically.")


@dataclass
class VerifyResult:
    offers: list[Offer] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    fetched: Optional[FetchResult] = None


KINDS = {"multi_day_package", "single_activity", "activity_combo", "hotel_only", "flight_only", "other"}


def verify_candidate(llm: LLM, fetcher: Fetcher, ref: ReferencePackage, hit: SearchHit) -> VerifyResult:
    fr: FetchResult = fetcher.fetch(hit.url)
    if not fr.ok:
        return VerifyResult([lead_offer(hit, fr.error or "unknown error")], [], fr)
    text = fr.text
    page_words = " " + " ".join(_norm_words(text)) + " "
    try:
        data = llm.text_json(VERIFY_SYSTEM, build_prompt(ref, fr.url, text), max_tokens=6000)
    except AppError as e:
        if e.code == "config_error":
            raise
        log.warning("verify failed for %s: %s", hit.url, e.message)
        return VerifyResult([lead_offer(hit, f"analysis failed ({e.message[:80]})")], fr.links, fr)
    raw_offers = data.get("offers")
    if not isinstance(raw_offers, list):
        return VerifyResult([], fr.links, fr)
    page_risky = [p for p in RISK_PHRASES if p in text.lower()]
    dom = domain_of(fr.url)
    nights_set, days_set, star_set = stated_nights(text), stated_days(text), stated_stars(text)
    out: list[Offer] = []
    for ro in raw_offers[:6]:
        if not isinstance(ro, dict):
            continue
        flags: list[str] = []
        matches: dict[str, str] = {}
        evidence: dict[str, str] = {}
        by_name = {}
        for c in ro.get("components") or []:
            if isinstance(c, dict) and c.get("name"):
                by_name[" ".join(str(c["name"]).lower().split())] = c
        for rc in ref.components:
            if not rc.required:
                continue
            c = by_name.get(" ".join(rc.name.lower().split()))
            status = str(c.get("status", "not_stated")).lower() if c else "not_stated"
            ev = str(c.get("evidence", "")).strip() if c else ""
            if status not in ("yes", "no", "not_stated"):
                status = "not_stated"
            if status == "yes" and not evidence_in_text(ev, page_words):
                status = "not_stated"  # no verifiable quote -> do not trust the claim
                flags.append(f"Unverified claim downgraded: {rc.name}")
            matches[rc.name] = status
            if ev and status != "not_stated":
                evidence[rc.name] = ev[:300]
        price_from, price_to = _f(ro.get("price_from")), _f(ro.get("price_to"))
        verified = price_in_text(price_from, text) if price_from is not None else False
        if price_from is not None and not verified:
            flags.append("Price not found verbatim on the page")
        if price_from is None:
            flags.append("No price shown on page")
        if page_risky:
            flags.append(f"Page warns: {page_risky[0]}")
        st = ro.get("seller_type")
        transfers = ro.get("transfers")
        extras, extra_ev = clean_extras(ro.get("extras"), ref, page_words)
        kind = str(ro.get("offer_kind") or ("multi_day_package" if (ro.get("nights") or ro.get("days")) else "other"))
        scope = str(ro.get("price_scope") or "unclear")
        star = _i(ro.get("hotel_star"))
        offer = Offer(
            seller=str(ro.get("seller") or dom)[:120], seller_type=st if st in SELLER_TYPES else "Other",
            listed_on=dom, package_name=str(ro.get("package_name") or "")[:200], hotel=ro.get("hotel"),
            hotel_star=grounded(star if star in (1, 2, 3, 4, 5) else None, star_set, "Hotel class", flags),
            nights=grounded(_i(ro.get("nights")), nights_set, "Nights", flags),
            days=grounded(_i(ro.get("days")), days_set, "Days", flags), destination=ro.get("destination"),
            price_from=price_from, price_to=price_to if price_to != price_from else None,
            currency=str(ro.get("currency") or "USD")[:3], price_basis=str(ro.get("price_basis") or "unknown"),
            price_pax=_i(ro.get("price_pax")), flights_included=_b(ro.get("flights_included")),
            transfers=None if transfers in (None, "not_stated") else str(transfers),
            visa_included=_b(ro.get("visa_included")), component_matches=matches, evidence=evidence,
            extras=extras, extra_evidence=extra_ev,
            offer_kind=kind if kind in KINDS else "other",
            price_scope=scope if scope in ("entire_package", "single_item", "unclear") else "unclear",
            price_verified=verified, verification="page", flags=flags, notes=str(ro.get("notes") or "")[:500],
            url=fr.url, availability_dates=ro.get("availability_dates"),
        )
        out.append(offer)
    return VerifyResult(out, fr.links, fr)
