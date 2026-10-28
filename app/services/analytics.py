"""Aggregate numbers shown in the UI and the Excel Summary sheet."""
from __future__ import annotations

import statistics
from typing import Any, Optional

from ..models import Offer, ReferencePackage


def _round(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


def gap_analytics(ref: ReferencePackage, offers: list[Offer], ref_usd: Optional[float]) -> dict[str, Any]:
    """Component coverage across competitors, commonly-offered extras, and the price-vs-content verdict."""
    pool = [o for o in offers if o.verification == "page" and o.match_level in ("same", "near", "partial")]
    comps = [c.name for c in ref.components if c.required]
    coverage = [{"name": n, "have": sum(1 for o in pool if o.component_matches.get(n) == "yes"), "total": len(pool)} for n in comps]
    ex: dict[str, dict[str, Any]] = {}
    for o in pool:
        for name in o.extras:
            e = ex.setdefault(name.lower(), {"name": name, "count": 0, "prices": []})
            e["count"] += 1
            if o.price_usd_from is not None and o.price_verified:
                e["prices"].append(o.price_usd_from)
    top = sorted(ex.values(), key=lambda e: -e["count"])[:20]
    extras = [{"name": e["name"], "count": e["count"], "median_usd": _round(statistics.median(e["prices"])) if e["prices"] else None}
              for e in top]
    priced = [o for o in pool if o.value_verdict]
    kinds: dict[str, int] = {}
    for o in priced:
        kinds[o.value_verdict] = kinds.get(o.value_verdict, 0) + 1
    text = None
    level = "info"
    lfl = [o for o in priced if o.components_total and o.components_matched == o.components_total]
    if lfl and ref_usd:
        prices = sorted(o.price_usd_from for o in lfl)
        cheaper = sum(1 for o in lfl if o.value_verdict.startswith("cheaper"))
        pricier = sum(1 for o in lfl if o.value_verdict.startswith("pricier"))
        med = statistics.median(prices)
        if cheaper / len(lfl) >= 0.66:
            level, text = "warning", (f"Your price looks HIGH: {cheaper} of {len(lfl)} competitors that show everything you offer charge less "
                                      f"(their median ${med:,.0f} vs your ${ref_usd:,.0f}).")
        elif pricier / len(lfl) >= 0.66:
            level, text = "success", (f"Your price looks COMPETITIVE: {pricier} of {len(lfl)} competitors that show everything you offer charge more "
                                      f"(their median ${med:,.0f} vs your ${ref_usd:,.0f}).")
        else:
            text = (f"Your price is IN LINE with the market: among {len(lfl)} competitors that show everything you offer, {cheaper} charge less "
                    f"and {pricier} charge more (median ${med:,.0f}).")
    elif priced and ref_usd:
        pl, cl = kinds.get("pricier_less", 0), kinds.get("cheaper_less", 0)
        text = (f"No competitor shows ALL your components. {pl} of {len(priced)} priced competitors charge more than you while showing fewer of your "
                f"components; {cl} charge less but for less content.")
    return {"component_coverage": coverage, "top_extras": extras, "value_kinds": kinds, "value_verdict_text": text,
            "value_verdict_level": level, "gap_pool": len(pool)}


def compute_analytics(ref: ReferencePackage, offers: list[Offer], ref_price_usd: Optional[float]) -> dict[str, Any]:
    page_offers = [o for o in offers if o.verification == "page"]
    leads = [o for o in offers if o.verification == "snippet"]
    stat_prices = sorted(o.price_usd_from for o in offers if o.in_stats and o.price_usd_from is not None)
    closest_count = len(stat_prices)
    basis = "closest"
    if not stat_prices:  # no Same/Near offer: compare against every whole package that has a verified price
        stat_prices = sorted(o.price_usd_from for o in offers if o.in_wide and o.price_usd_from is not None)
        basis = "all_priced"
    wide = sorted(o.price_usd_from for o in offers if o.in_wide and o.price_usd_from is not None)
    out: dict[str, Any] = {
        "wide_count": len(wide),
        "wide_min": _round(wide[0]) if wide else None,
        "wide_max": _round(wide[-1]) if wide else None,
        "wide_median": _round(statistics.median(wide)) if wide else None,
        "reference_price_usd": _round(ref_price_usd),
        "total_found": len(offers),
        "verified_pages": len(page_offers),
        "same": sum(1 for o in offers if o.match_level == "same"),
        "leads_to_check": len(leads),
        "priced": sum(1 for o in page_offers if o.price_usd_from is not None),
        "in_stats": closest_count,
        "stats_basis": basis,
        "stats_count": len(stat_prices),
        "flights_included": sum(1 for o in page_offers if o.flights_included is True),
        "by_level": {lv: sum(1 for o in page_offers if o.match_level == lv) for lv in ("same", "near", "partial", "rejected")},
        "by_seller_type": {},
        "by_domain": {},
    }
    for o in page_offers:
        out["by_seller_type"][o.seller_type] = out["by_seller_type"].get(o.seller_type, 0) + 1
        out["by_domain"][o.listed_on] = out["by_domain"].get(o.listed_on, 0) + 1

    out.update(gap_analytics(ref, offers, ref_price_usd))
    if stat_prices:
        median = statistics.median(stat_prices)
        out.update(
            min=_round(stat_prices[0]), max=_round(stat_prices[-1]), median=_round(median),
            mean=_round(statistics.fmean(stat_prices)),
            p25=_round(statistics.quantiles(stat_prices, n=4)[0]) if len(stat_prices) >= 2 else _round(stat_prices[0]),
            p75=_round(statistics.quantiles(stat_prices, n=4)[2]) if len(stat_prices) >= 2 else _round(stat_prices[0]),
        )
        if ref_price_usd:
            out["vs_median_usd"] = _round(ref_price_usd - median)
            out["vs_median_pct"] = round((ref_price_usd - median) / median * 100, 1) if median else None
            cheaper = sum(1 for p in stat_prices if p < ref_price_usd)
            out["rank"] = cheaper + 1
            out["rank_of"] = len(stat_prices) + 1
            out["position"] = (
                "below market" if ref_price_usd < out["p25"] else
                "above market" if ref_price_usd > out["p75"] else "within the market's middle range"
            )
    return out
