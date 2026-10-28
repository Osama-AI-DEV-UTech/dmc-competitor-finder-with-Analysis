import pytest

from app.errors import ExtractionError
from app.models import AnalysisOptions, Offer, ReferencePackage
from app.services.extractor import coerce_reference
from app.services.llm import SearchHit, parse_json_object
from app.services.matcher import dedupe_offers, score_offer
from app.services.searcher import normalize_url, select_candidates
from app.services.verifier import _norm_words, evidence_in_text, price_in_text
from tests.conftest import REF_JSON


def ref():
    return ReferencePackage(**REF_JSON)


def offer(**kw):
    ref_ = ref()
    base = dict(seller="S", package_name="Dubai 4N", nights=4, days=5, hotel_star=4, destination="Dubai",
                price_from=500, currency="USD", price_basis="per person", flights_included=False, transfers="SIC",
                price_verified=True, verification="page", url="https://a.com/x",
                component_matches={c.name: "yes" for c in ref_.components})
    base.update(kw)
    return Offer(**base)


def test_price_in_text():
    t = "INR 43,780 and $437.00 and 12,34,567 and 99.5"
    assert price_in_text(43780, t) and price_in_text(437, t) and price_in_text(1234567, t) and price_in_text(99.5, t)
    assert not price_in_text(4378, t) and not price_in_text(99, t) and not price_in_text(None, t)


def test_evidence():
    words = " " + " ".join(_norm_words("Includes a Desert Safari with BBQ dinner, and more")) + " "
    assert evidence_in_text("desert safari with bbq dinner", words)
    assert not evidence_in_text("miracle garden", words)
    assert not evidence_in_text("", words)


def test_parse_json_object():
    assert parse_json_object('x ```json\n{"a": 1}\n``` y') == {"a": 1}
    with pytest.raises(ExtractionError):
        parse_json_object("no json here")
    with pytest.raises(ExtractionError):
        parse_json_object("")


def test_normalize_and_select():
    assert normalize_url("HTTPS://Www.X.com/a/?utm_source=1&id=2#frag") == "https://www.x.com/a?id=2"
    hits = [SearchHit(u) for u in [
        "https://www.ubidtours.com/p", "https://sub.ubidtours.com/p", "https://facebook.com/x", "https://a.com/f.pdf",
        "https://a.com/1", "https://a.com/1?utm_x=1", "ftp://x.com/y", "https://b.com/2"]]
    out = select_candidates(hits, ["ubidtours.com"], 10)
    assert [h.url for h in out] == ["https://a.com/1", "https://b.com/2"]
    many = [SearchHit(f"https://same.com/{i}") for i in range(20)]
    assert len(select_candidates(many, [], 50)) == 6  # per-domain cap


def test_coerce_reference():
    r = coerce_reference({"title": "T", "destination": "Dubai", "nights": "4", "days": "5 days", "hotel_star": "4-star",
                          "price_amount": "$ 1,511", "transfers": "sharing basis", "flights_included": None,
                          "components": ["Safari", {"name": "Safari"}, {"name": "Cruise", "category": "activity"}, {"foo": 1}]})
    assert (r.nights, r.days, r.hotel_star, r.price_amount, r.transfers) == (4, 5, 4, 1511.0, "SIC")
    assert [c.name for c in r.components] == ["Safari", "Cruise"]
    assert r.flights_included is False
    assert coerce_reference({"destination": "X", "hotel_star": 9, "components": ["a"]}).hotel_star is None


def test_same_in_stats(fx):
    o = score_offer(ref(), offer(price_scope="entire_package"), fx, AnalysisOptions(), 511)
    assert o.match_level == "same" and o.in_stats and o.price_usd_from == 500 and o.missing == []


def test_single_activity_rejected(fx):
    """The reported bug: a desert safari page must never count as the package."""
    o = score_offer(ref(), offer(offer_kind="single_activity", price_scope="single_item", price_from=300,
                                 component_matches={"Desert safari with BBQ dinner": "yes"}), fx, AnalysisOptions(), 511)
    assert o.match_level == "rejected" and not o.in_stats and "activity" in o.reject_reason.lower()
    o = score_offer(ref(), offer(price_scope="single_item"), fx, AnalysisOptions(), 511)
    assert o.match_level == "rejected" and "single item" in o.reject_reason


def test_missing_component_is_not_same(fx):
    names = [c.name for c in ref().components]
    cm = {n: ("yes" if i < 5 else "not_stated") for i, n in enumerate(names)}
    o = score_offer(ref(), offer(component_matches=cm), fx, AnalysisOptions(), 511)
    assert o.match_level == "near" and o.in_stats and o.missing == [names[5]]  # closest match is used for comparison
    o = score_offer(ref(), offer(component_matches=cm), fx, AnalysisOptions(strictness="strict"), 511)
    assert o.match_level == "near" and not o.in_stats  # strict: exact packages only in the statistics

    cm[names[5]] = "no"
    o = score_offer(ref(), offer(component_matches=cm), fx, AnalysisOptions(), 511)
    assert o.match_level == "partial" and o.missing == [names[5] + " (excluded)"]


def test_strictness(fx):
    names = [c.name for c in ref().components]
    cm = {n: ("yes" if i < 5 else "not_stated") for i, n in enumerate(names)}
    assert score_offer(ref(), offer(component_matches=cm), fx, AnalysisOptions(strictness="relaxed"), 511).match_level == "near"
    half = {n: ("yes" if i < 3 else "not_stated") for i, n in enumerate(names)}  # 50%
    assert score_offer(ref(), offer(component_matches=half), fx, AnalysisOptions(), 511).match_level == "partial"
    assert score_offer(ref(), offer(component_matches=half), fx, AnalysisOptions(strictness="relaxed"), 511).match_level == "near"
    assert score_offer(ref(), offer(hotel_star=None), fx, AnalysisOptions(strictness="strict"), 511).match_level == "near"
    assert score_offer(ref(), offer(hotel_star=None), fx, AnalysisOptions(), 511).match_level == "same"
    o = score_offer(ref(), offer(nights=None, days=None), fx, AnalysisOptions(), 511)
    assert o.match_level == "near" and o.in_stats


def test_per_couple_and_fx(fx):
    o = score_offer(ref(), offer(price_from=86000, currency="INR", price_basis="per couple"), fx, AnalysisOptions(), 511)
    assert o.price_usd_from == round(86000 * 0.0114 / 2, 2)
    assert any("divided by 2" in f for f in o.flags)


def test_group_price_needs_pax(fx):
    o = score_offer(ref(), offer(price_basis="total", price_pax=None), fx, AnalysisOptions(), 511)
    assert not o.in_stats
    o2 = score_offer(ref(), offer(price_basis="total", price_pax=4, price_from=2000), fx, AnalysisOptions(), 511)
    assert o2.price_usd_from == 500 and o2.in_stats


def test_flights_bundle_is_near_and_excluded(fx):
    o = score_offer(ref(), offer(flights_included=True), fx, AnalysisOptions(), 511)
    assert o.match_level == "near" and not o.in_stats
    o = score_offer(ref(), offer(flights_included=True), fx, AnalysisOptions(include_flight_packages_in_stats=True), 511)
    assert o.match_level == "same" and o.in_stats


def test_mismatches(fx):
    assert score_offer(ref(), offer(nights=3), fx, AnalysisOptions(), 511).match_level == "partial"
    assert score_offer(ref(), offer(hotel_star=5), fx, AnalysisOptions(), 511).match_level == "partial"
    assert score_offer(ref(), offer(transfers="private"), fx, AnalysisOptions(), 511).match_level == "near"
    o = score_offer(ref(), offer(destination="Bali", package_name="Bali trip"), fx, AnalysisOptions(), 511)
    assert o.match_level == "rejected" and not o.in_stats


def test_price_outlier_and_unverified(fx):
    o = score_offer(ref(), offer(price_from=82), fx, AnalysisOptions(), 511)
    assert o.match_level == "same" and o.in_stats and any("very different from yours" in f for f in o.flags)
    assert not score_offer(ref(), offer(price_verified=False), fx, AnalysisOptions(), 511).in_stats
    assert not score_offer(ref(), offer(flags=["Page warns: may not be available"]), fx, AnalysisOptions(), 511).in_stats
    assert not score_offer(ref(), offer(price_from=None), fx, AnalysisOptions(), 511).in_stats
    o = score_offer(ref(), offer(currency="ZZZ"), fx, AnalysisOptions(), 511)
    assert not o.in_stats and any("No FX rate" in f for f in o.flags)


def test_low_overlap_still_listed_with_price(fx):
    names = [c.name for c in ref().components]
    o = score_offer(ref(), offer(component_matches={n: ("yes" if i < 2 else "not_stated") for i, n in enumerate(names)}), fx, AnalysisOptions(), 511)
    # few components proven, but it is a whole package with a price: shown as partial, in the wide range only
    assert o.match_level == "partial" and not o.in_stats and o.in_wide and len(o.missing) == 4


def test_grounding_regex():
    from app.services.verifier import stated_days, stated_nights, stated_stars
    t = "Dubai 4 Nights / 5 Days, 4 star hotel; 4N/5D; 5-Star"
    assert stated_nights(t) == {4} and stated_days(t) == {5} and stated_stars(t) == {4, 5}
    assert stated_nights("price 2024 night out") == set()


def test_dedupe_keeps_best():
    a = offer(match_score=50)
    b = offer(match_score=80)
    assert dedupe_offers([a, b])[0].match_score == 80
    assert len(dedupe_offers([offer(url="u1", verification="snippet"), offer(url="u2", verification="snippet")])) == 2


def test_transfers_merged_into_one_component():
    r = coerce_reference({"destination": "China", "components": [
        {"name": "transfer: airport to hotel", "category": "transfer"}, {"name": "Great Wall visit", "category": "attraction"},
        {"name": "transfer: hotel to Bund", "category": "transfer"}, {"name": "transfer: hotel to airport", "category": "transfer"}]})
    names = [c.name for c in r.components]
    assert len(names) == 2 and sum(c.category == "transfer" for c in r.components) == 1 and "Great Wall visit" in names


def test_analytics_falls_back_to_all_priced(fx):
    from app.services.analytics import compute_analytics

    names = [c.name for c in ref().components]
    cm = {n: ("yes" if i < 3 else "not_stated") for i, n in enumerate(names)}
    o = score_offer(ref(), offer(component_matches=cm), fx, AnalysisOptions(), 511)
    assert o.match_level == "partial" and not o.in_stats and o.in_wide
    a = compute_analytics(ref(), [o], 511)
    assert a["in_stats"] == 0 and a["stats_basis"] == "all_priced" and a["median"] == 500 and a["rank"] == 2
