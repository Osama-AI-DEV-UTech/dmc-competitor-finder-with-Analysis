import io

import openpyxl
import pytest

from app.errors import Conflict, NotFound, ValidationFailed
from app.models import AnalysisOptions, ReferencePackage
from tests.conftest import make_png, wait_for


def run(app, review=False, **opt):
    aid = app.create_analysis("shot.png", make_png(), AnalysisOptions(review_before_search=review, **opt))
    return aid


def by_url(offers):
    return {o.url: o for o in offers}


def test_full_run_strict_matching(app_service):
    aid = run(app_service)
    a = wait_for(app_service, aid, {"completed", "failed"})
    assert a["status"] == "completed", a["message"]
    o = by_url(app_service.offers(aid))
    # genuinely same packages (all six components proven, whole-package price)
    assert o["https://exact.example.com/dubai-5d4n"].match_level == "same"
    assert o["https://exact.example.com/dubai-5d4n"].in_stats
    # found through link expansion from a listing page
    assert o["https://list.example.com/dubai-4-nights-5-days-package"].match_level == "same"
    # extras: quoted + genuinely new is kept; unquoted or reworded-reference extras are dropped
    ex = o["https://exact.example.com/dubai-5d4n"]
    assert ex.extras == ["Abu Dhabi city tour"] and "Abu Dhabi" in ex.extra_evidence["Abu Dhabi city tour"]
    assert "Abu Dhabi city tour" in ex.value_note and ex.value_verdict == "cheaper_more"
    cl = o["https://close.example.in/dubai"]
    assert cl.value_verdict == "cheaper_less" and "Miracle Garden visit" in cl.value_note
    # THE BUG: a single desert safari is rejected, never a competitor
    fbt = o["https://fbt.example.com/dubai-desert-safari"]
    assert fbt.match_level == "rejected" and not fbt.in_stats and fbt.reject_reason
    # unproven Miracle Garden -> near, with the missing component named
    close = o["https://close.example.in/dubai"]
    assert close.match_level == "near" and close.missing == ["Miracle Garden visit"] and close.in_stats
    # nights/stars the LLM invented are dropped -> near, not same
    vague = o["https://vague.example.com/dubai"]
    assert vague.match_level == "near" and vague.nights is None and vague.in_stats
    # flights bundled / invented price / different length never reach the stats
    assert not o["https://flights.example.com/dubai"].in_stats
    assert o["https://flights.example.com/dubai"].match_level == "near"
    inv = o["https://invented.example.com/dubai"]
    assert not inv.price_verified and not inv.in_stats
    assert o["https://short.example.com/dubai-3n"].match_level in ("partial", "rejected")
    # filtered urls never appear
    assert not any("ubidtours" in u or "facebook" in u or u.endswith(".pdf") for u in o)
    # stats only from same + verified
    s = a["summary"]
    assert s["value_verdict_text"] and s["component_coverage"][0]["total"] >= 5
    assert any(e["name"] == "Abu Dhabi city tour" for e in s["top_extras"])
    assert s["value_kinds"].get("cheaper_more", 0) >= 2
    assert s["same"] == 3 and s["in_stats"] == 4 and s["min"] == 437 and round(s["max"]) == 490
    assert any(x.verification == "snippet" for x in o.values())  # blocked page listed as a lead


def test_review_flow_and_export(app_service):
    aid = run(app_service, review=True)
    a = wait_for(app_service, aid, {"awaiting_review", "failed"})
    assert a["status"] == "awaiting_review" and len(a["reference"]["components"]) == 6
    ref = ReferencePackage(**a["reference"])
    app_service.start(aid, ref)
    a = wait_for(app_service, aid, {"completed", "failed"})
    assert a["status"] == "completed"
    xlsx = app_service.export_xlsx(aid)
    wb = openpyxl.load_workbook(io.BytesIO(xlsx), data_only=True)
    assert wb.sheetnames == ["Summary", "Closest Matches", "Partial Matches", "Gap Analysis", "Rejected", "Pages To Check Manually",
                             "Evidence", "Reference Package", "Assumptions"]
    ws = wb["Closest Matches"]
    sellers = [ws.cell(r, 2).value for r in range(5, ws.max_row + 1) if ws.cell(r, 2).value]
    assert {"Exact Tours", "Invented Price Ltd", "Listing Tours", "Close Travels", "Vague Tours"} <= set(sellers)
    rej = wb["Rejected"]
    assert any("single" in str(rej.cell(r, 7).value).lower() for r in range(4, rej.max_row + 1))
    gap = wb["Gap Analysis"]
    cells = [str(c.value) for row in gap.iter_rows() for c in row if c.value is not None]
    assert any("Abu Dhabi city tour" in c for c in cells) and any("Miracle Garden visit" in c for c in cells)
    assert any("How many competitors show each of YOUR components" in c for c in cells)
    assert any("Miracle Garden" in str(c.value) for row in ws.iter_rows() for c in row)  # gap named for the near match
    assert wb["Summary"]["B6"].value >= 5  # comparable packages (same + near)


def test_validation_and_errors(app_service, settings):
    with pytest.raises(ValidationFailed):
        app_service.create_analysis("x.png", b"")
    with pytest.raises(ValidationFailed):
        app_service.create_analysis("x.png", b"not an image")
    with pytest.raises(NotFound):
        app_service.get("../../etc/passwd")
    with pytest.raises(NotFound):
        app_service.get("0" * 12)
    aid = run(app_service, review=True)
    wait_for(app_service, aid, {"awaiting_review"})
    with pytest.raises(Conflict):
        app_service.export_xlsx(aid)
    app_service.delete(aid)
    with pytest.raises(NotFound):
        app_service.get(aid)


def test_missing_openai_key(settings, fx):
    from app.config import Settings
    from app.core import AppService
    from app.db import Database
    from app.errors import ConfigError
    from app.services.search_providers import MultiSearch
    from tests.conftest import FakeEngine, FakeFetcher, FakeLLM

    s = Settings(openai_api_key="", db_path=settings.db_path, live_fx=False)
    svc = AppService(s, Database(s.db_file), FakeLLM(), MultiSearch([FakeEngine()]), FakeFetcher(), fx)
    with pytest.raises(ConfigError):
        svc.create_analysis("x.png", make_png())


def test_all_search_engines_failing_fails_cleanly(settings, fx):
    from app.core import AppService
    from app.db import Database
    from app.services.search_providers import MultiSearch
    from tests.conftest import FakeEngine, FakeFetcher, FakeLLM

    svc = AppService(settings, Database(settings.db_file), FakeLLM(), MultiSearch([FakeEngine(fail=True)]), FakeFetcher(), fx)
    aid = svc.create_analysis("x.png", make_png(), AnalysisOptions(review_before_search=False))
    a = wait_for(svc, aid, {"completed", "failed"})
    assert a["status"] == "failed" and "search" in a["error"].lower()
    svc.retry(aid)
