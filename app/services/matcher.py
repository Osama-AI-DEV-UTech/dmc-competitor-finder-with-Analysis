"""Deterministic scoring: how close is an offer to the reference, and may it enter the price statistics?"""
from __future__ import annotations

import re
from typing import Optional

from ..models import AnalysisOptions, Offer, ReferencePackage
from .currency import CurrencyService

NOTSTATED = "not_stated"


def _tokens(s: Optional[str]) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2}


def destination_ok(ref: ReferencePackage, offer: Offer) -> Optional[bool]:
    """True/False when we can tell, None when the offer gives no destination information."""
    want = _tokens(ref.destination) | {t for c in ref.cities for t in _tokens(c)}
    have = _tokens(offer.destination) | _tokens(offer.package_name)
    if not want:
        return None
    if not have:
        return None
    return bool(want & have)


def per_person_factor(offer: Offer) -> tuple[float, Optional[str]]:
    """Return (divisor, problem). problem is set when the price basis cannot be resolved."""
    basis = (offer.price_basis or "unknown").lower()
    if basis == "per person":
        return 1.0, None
    if basis == "per couple":
        return 2.0, None
    if basis in ("per group", "total"):
        if offer.price_pax and offer.price_pax > 0:
            return float(offer.price_pax), None
        return 1.0, "Price is for a group/total but the number of travellers is unknown"
    return 1.0, "Price basis not stated (assumed per person)"


LEVEL_ORDER = {"same": 0, "near": 1, "partial": 2, "rejected": 3}


def value_assessment(ref: ReferencePackage, offer: Offer, ref_usd: Optional[float], flights_note: bool) -> None:
    """Is this competitor cheaper/pricier, and what do they give for it compared with you?"""
    offer.value_verdict, offer.value_note = "", ""
    if not ref_usd or offer.price_usd_from is None or offer.match_level == "rejected" or not offer.price_verified:
        return
    diff = offer.price_usd_from - ref_usd
    pct = diff / ref_usd
    total, have = offer.components_total, offer.components_matched
    lacks = [m for m in offer.missing]
    gives_all = total > 0 and have == total
    money = f"${abs(diff):,.0f} ({abs(pct) * 100:.0f}%)"
    plus = (f" and offers {len(offer.extras)} extra(s) you do not: {', '.join(offer.extras[:3])}{'…' if len(offer.extras) > 3 else ''}"
            if offer.extras else "")
    lack_txt = f"does not show {len(lacks)} of your {total} components" + (f" ({', '.join(lacks[:3])}{'…' if len(lacks) > 3 else ''})" if lacks else "")
    if abs(pct) <= 0.03:
        kind = "similar"
        note = "Within 3% of your price; " + ("shows all your components" + plus if gives_all else lack_txt + plus)
    elif diff < 0:
        kind = "cheaper_more" if gives_all else "cheaper_less"
        note = (f"Cheaper by {money} and shows all your components" + plus) if gives_all else (f"Cheaper by {money} but {lack_txt}" + plus)
    else:
        kind = "pricier_more" if gives_all else "pricier_less"
        note = (f"Pricier by {money}; shows all your components" + (plus if plus else " (same content)")) if gives_all else \
            (f"Pricier by {money} and {lack_txt}" + plus)
    if flights_note:
        note += " - includes flights"
    offer.value_verdict, offer.value_note = kind, note


def score_offer(ref: ReferencePackage, offer: Offer, fx: CurrencyService, opts: AnalysisOptions,
                ref_usd: Optional[float] = None, low: float = 0.35, high: float = 3.0) -> Offer:
    """Decide how close the offer is to the reference. 'same' requires EVERY component plus a whole-package price."""
    flags = list(offer.flags)
    comps = [c.name for c in ref.components if c.required]
    total = len(comps)
    status = {c: offer.component_matches.get(c, NOTSTATED) for c in comps}
    yes = sum(1 for v in status.values() if v == "yes")
    ratio = (yes / total) if total else 0.0
    offer.components_matched, offer.components_total = yes, total
    offer.missing = [f"{c} (excluded)" if v == "no" else c for c, v in status.items() if v != "yes"]
    any_no = any(v == "no" for v in status.values())

    # ---- price normalisation (per person, USD)
    divisor, basis_problem = per_person_factor(offer)
    unresolved = bool(basis_problem and "unknown" in basis_problem and "group" in basis_problem.lower())
    if basis_problem:
        flags.append(basis_problem)
    usd_from = fx.to_usd(offer.price_from, offer.currency)
    usd_to = fx.to_usd(offer.price_to, offer.currency)
    if offer.price_from is not None and usd_from is None:
        flags.append(f"No FX rate for {offer.currency}; price not converted")
    offer.price_usd_from = round(usd_from / divisor, 2) if usd_from is not None else None
    offer.price_usd_to = round(usd_to / divisor, 2) if usd_to is not None else None
    if divisor != 1.0:
        flags.append(f"Price divided by {divisor:g} to get a per-person figure")

    # ---- hard comparisons
    nights_mismatch = bool(ref.nights and offer.nights and ref.nights != offer.nights)
    if not nights_mismatch and ref.days and offer.days and not offer.nights and ref.days != offer.days:
        nights_mismatch = True
    nights_unstated = bool(ref.nights and offer.nights is None and not (ref.days and offer.days == ref.days))
    star_mismatch = bool(ref.hotel_star and offer.hotel_star and ref.hotel_star != offer.hotel_star)
    star_unstated = bool(ref.hotel_star and offer.hotel_star is None)
    dest = destination_ok(ref, offer)
    transfers_mismatch = bool(ref.transfers and offer.transfers and offer.transfers not in ("mixed",)
                              and ref.transfers.lower() != offer.transfers.lower())
    flights_extra = (offer.flights_included is True and not ref.flights_included
                     and not opts.include_flight_packages_in_stats)
    if nights_mismatch:
        flags.append(f"Different length ({offer.nights or offer.days} vs {ref.nights or ref.days})")
    if star_mismatch:
        flags.append(f"Different hotel class ({offer.hotel_star}-star vs {ref.hotel_star}-star)")
    if dest is False:
        flags.append("Different destination")
    if transfers_mismatch:
        flags.append(f"Transfers differ ({offer.transfers} vs {ref.transfers})")
    if offer.flights_included is True and not ref.flights_included:
        flags.append("Flights included (reference is land-only)")

    # ---- numeric score (for sorting) 0-100
    score = 70.0 * ratio
    score += 10 if (ref.nights and offer.nights and not nights_mismatch) else (4 if not (ref.nights and offer.nights) else 0)
    score += 10 if (ref.hotel_star and offer.hotel_star and not star_mismatch) else (4 if not (ref.hotel_star and offer.hotel_star) else 0)
    score += 5 if (ref.transfers and offer.transfers and not transfers_mismatch) else (2 if not (ref.transfers and offer.transfers) else 0)
    score += 5 if dest is True else (2 if dest is None else 0)
    offer.match_score = round(score, 1)

    # ---- level: gates first
    strict = opts.strictness
    reject = ""
    if offer.verification == "snippet":
        reject = "Page could not be read"
    elif offer.offer_kind != "multi_day_package":
        reject = {"single_activity": "Single activity, not a package", "activity_combo": "Activity combo, not a multi-day package",
                  "hotel_only": "Hotel only", "flight_only": "Flight only"}.get(offer.offer_kind, "Not a multi-day package")
    elif offer.price_scope == "single_item":
        reject = "Price is for a single item, not the whole package"
    elif dest is False:
        reject = "Different destination"
    elif total == 0:
        reject = "Reference has no components to compare"

    near_min = {"strict": 0.8, "standard": 0.7, "relaxed": 0.5}[strict]
    if reject:
        level = "rejected"
        offer.reject_reason = reject
    else:
        all_yes = yes == total
        hard_conflict = nights_mismatch or star_mismatch or any_no
        full = (all_yes and not hard_conflict and not transfers_mismatch and not flights_extra
                and offer.price_scope == "entire_package" and not nights_unstated)
        if strict == "strict":
            full = full and not star_unstated
        if full:
            level = "same"
        elif ratio >= near_min and not hard_conflict:
            level = "near"
        else:
            level = "partial"
        offer.reject_reason = ""
    offer.match_level = level  # type: ignore[assignment]

    # ---- statistics eligibility: whole packages only, with a verified price
    outlier = False
    if ref_usd and offer.price_usd_from:
        outlier = offer.price_usd_from < low * ref_usd or offer.price_usd_from > high * ref_usd
        if outlier:
            flags.append("Price very different from yours - double-check the page")
    risky = any(f.startswith("Page warns") for f in flags)
    flights_block = flights_extra
    # strict: exact packages only; standard/relaxed: exact + closest matches (still a whole package, never a single item)
    eligible = ("same",) if strict == "strict" else ("same", "near")
    price_ok = bool(offer.verification == "page" and offer.price_verified and offer.price_usd_from is not None
                    and not risky and not unresolved)
    offer.in_wide = bool(price_ok and level != "rejected" and not flights_block)
    offer.in_stats = bool(offer.in_wide and level in eligible)
    offer.flags = list(dict.fromkeys(flags))
    value_assessment(ref, offer, ref_usd, offer.flights_included is True and not ref.flights_included)
    return offer


def dedupe_offers(offers: list[Offer]) -> list[Offer]:
    """Collapse the same offer found on several pages; keep the best-evidenced copy."""
    best: dict[tuple, Offer] = {}
    for o in offers:
        if o.verification == "snippet":
            key = ("lead", o.url)
        else:
            key = (o.seller.lower().strip(), re.sub(r"\W+", "", o.package_name.lower())[:60], o.price_from, (o.hotel or "").lower())
        cur = best.get(key)
        if cur is None or (o.match_score, o.price_verified) > (cur.match_score, cur.price_verified):
            best[key] = o
    return list(best.values())
