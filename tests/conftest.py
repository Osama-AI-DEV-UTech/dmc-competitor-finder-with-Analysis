import io
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.core import AppService  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import UpstreamError  # noqa: E402
from app.services.currency import CurrencyService  # noqa: E402
from app.services.fetcher import FetchResult  # noqa: E402
from app.services.llm import SearchHit  # noqa: E402
from app.services.search_providers import MultiSearch  # noqa: E402

REF_JSON = {
    "title": "Dubai Holiday Package", "destination": "Dubai", "country": "UAE", "cities": ["Dubai"],
    "nights": 4, "days": 5, "hotel_star": 4, "hotel_names": [], "price_amount": 511, "price_currency": "USD",
    "price_basis": "per person", "operator": "Waguru Tours", "transfers": "SIC", "flights_included": False,
    "components": [
        {"name": "4-star hotel (4 nights)", "category": "hotel"},
        {"name": "Daily breakfast", "category": "meal"},
        {"name": "Desert safari with BBQ dinner", "category": "activity"},
        {"name": "Marina dhow cruise dinner", "category": "activity"},
        {"name": "Burj Khalifa 124th floor", "category": "attraction"},
        {"name": "Miracle Garden visit", "category": "attraction"},
    ],
}

PAGES = {
    # exact match, per person
    "https://exact.example.com/dubai-5d4n": {
        "text": "Dubai 5 Days 4 Nights package. Price: USD 437 per person. 4 star hotel for 4 nights. Daily breakfast included. "
                "Desert safari with BBQ dinner. Marina dhow cruise dinner. Burj Khalifa 124th floor entry. Miracle Garden visit. "
                "Airport transfers on sharing basis. Flights not included. Abu Dhabi city tour included. " * 3,
        "offers": [{
            "seller": "Exact Tours", "seller_type": "DMC", "package_name": "Dubai 5D/4N", "hotel": "Gevora", "hotel_star": 4,
            "nights": 4, "days": 5, "destination": "Dubai", "price_from": 437, "currency": "USD", "price_basis": "per person",
            "flights_included": False, "transfers": "SIC",
            "extras": [{"name": "Abu Dhabi city tour", "evidence": "Abu Dhabi city tour included"},
                       {"name": "Free spa voucher", "evidence": "complimentary spa voucher for every guest"},
                       {"name": "Miracle Garden entry", "evidence": "Miracle Garden visit"}],
            "components": [
                {"name": "4-star hotel (4 nights)", "status": "yes", "evidence": "4 star hotel for 4 nights"},
                {"name": "Daily breakfast", "status": "yes", "evidence": "Daily breakfast included"},
                {"name": "Desert safari with BBQ dinner", "status": "yes", "evidence": "Desert safari with BBQ dinner"},
                {"name": "Marina dhow cruise dinner", "status": "yes", "evidence": "Marina dhow cruise dinner"},
                {"name": "Burj Khalifa 124th floor", "status": "yes", "evidence": "Burj Khalifa 124th floor entry"},
                {"name": "Miracle Garden visit", "status": "yes", "evidence": "Miracle Garden visit"},
            ]}],
    },
    # close match, hallucinated claims + price in INR per couple
    "https://close.example.in/dubai": {
        "text": "Dubai tour 4N/5D. INR 86,000 for 2 persons. 4 star hotel for 4 nights with breakfast. Desert safari with BBQ dinner. "
                "Dhow marina dinner. Burj Khalifa 124th floor. " * 3 + " padding " * 400,
        "offers": [{
            "seller": "Close Travels", "seller_type": "OTA", "package_name": "Dubai Highlights", "hotel_star": 4,
            "nights": 4, "days": 5, "destination": "Dubai", "price_from": 86000, "currency": "INR", "price_basis": "per couple",
            "flights_included": False, "transfers": "SIC",
            "components": [
                {"name": "4-star hotel (4 nights)", "status": "yes", "evidence": "4 star hotel for 4 nights"},
                {"name": "Daily breakfast", "status": "yes", "evidence": "with breakfast"},
                {"name": "Desert safari with BBQ dinner", "status": "yes", "evidence": "Desert safari with BBQ dinner"},
                {"name": "Marina dhow cruise dinner", "status": "yes", "evidence": "Dhow marina dinner"},
                {"name": "Burj Khalifa 124th floor", "status": "yes", "evidence": "Burj Khalifa 124th floor"},
                # LLM claims Miracle Garden but quotes text that is NOT on the page -> must be downgraded
                {"name": "Miracle Garden visit", "status": "yes", "evidence": "visit to the beautiful Miracle Garden flower park"},
            ]}],
    },
    # includes flights
    "https://flights.example.com/dubai": {
        "text": "Dubai package with return flights. USD 999 per person. 4 star hotel for 4 nights. Daily breakfast. "
                "Desert safari with BBQ dinner. Marina dhow cruise dinner. Burj Khalifa 124th floor. Miracle Garden visit. " * 3,
        "offers": [{
            "seller": "Flight Bundle Co", "seller_type": "Retailer", "package_name": "Dubai with flights", "hotel_star": 4,
            "nights": 4, "days": 5, "destination": "Dubai", "price_from": 999, "currency": "USD", "price_basis": "per person",
            "flights_included": True, "transfers": "private",
            "components": [{"name": n, "status": "yes", "evidence": e} for n, e in [
                ("4-star hotel (4 nights)", "4 star hotel for 4 nights"), ("Daily breakfast", "Daily breakfast"),
                ("Desert safari with BBQ dinner", "Desert safari with BBQ dinner"),
                ("Marina dhow cruise dinner", "Marina dhow cruise dinner"),
                ("Burj Khalifa 124th floor", "Burj Khalifa 124th floor"), ("Miracle Garden visit", "Miracle Garden visit")]]}],
    },
    # price invented by the LLM (not on page)
    "https://invented.example.com/dubai": {
        "text": "Dubai 4 nights package. Contact us for the best price. 4 star hotel for 4 nights. Daily breakfast. "
                "Desert safari with BBQ dinner. Marina dhow cruise dinner. Burj Khalifa 124th floor. Miracle Garden visit. " * 3,
        "offers": [{
            "seller": "Invented Price Ltd", "seller_type": "Tour operator", "package_name": "Dubai 4N", "hotel_star": 4,
            "nights": 4, "days": 5, "destination": "Dubai", "price_from": 123, "currency": "USD", "price_basis": "per person",
            "flights_included": False, "transfers": "SIC",
            "components": [{"name": n, "status": "yes", "evidence": e} for n, e in [
                ("4-star hotel (4 nights)", "4 star hotel for 4 nights"), ("Daily breakfast", "Daily breakfast"),
                ("Desert safari with BBQ dinner", "Desert safari with BBQ dinner"),
                ("Marina dhow cruise dinner", "Marina dhow cruise dinner"),
                ("Burj Khalifa 124th floor", "Burj Khalifa 124th floor"), ("Miracle Garden visit", "Miracle Garden visit")]]}],
    },
    # different length -> partial
    "https://short.example.com/dubai-3n": {
        "text": "Dubai 3 nights. USD 300 per person. 4 star hotel. Breakfast daily. Desert safari with BBQ dinner. " * 3,
        "offers": [{
            "seller": "Short Stay", "seller_type": "OTA", "package_name": "Dubai 3N", "hotel_star": 4, "nights": 3, "days": 4,
            "destination": "Dubai", "price_from": 300, "currency": "USD", "price_basis": "per person", "flights_included": False,
            "components": [
                {"name": "4-star hotel (4 nights)", "status": "yes", "evidence": "4 star hotel"},
                {"name": "Daily breakfast", "status": "yes", "evidence": "Breakfast daily"},
                {"name": "Desert safari with BBQ dinner", "status": "yes", "evidence": "Desert safari with BBQ dinner"}]}],
    },
    # single activity (the bug the user reported): same words, but it is only a desert safari
    "https://fbt.example.com/dubai-desert-safari": {
        "text": "Dubai Desert Safari with BBQ dinner. AED 300 per person. Pick up and drop off included. Desert safari with BBQ dinner. " * 3,
        "offers": [{
            "seller": "FBT DMC", "seller_type": "DMC", "package_name": "Dubai Desert Safari", "offer_kind": "single_activity",
            "price_scope": "single_item", "destination": "Dubai", "price_from": 300, "currency": "AED", "price_basis": "per person",
            "components": [{"name": "Desert safari with BBQ dinner", "status": "yes", "evidence": "Desert safari with BBQ dinner"}]}],
    },
    # complete package but the LLM invents nights/stars that the page never states
    "https://vague.example.com/dubai": {
        "text": "Dubai holiday package USD 450 per person. Hotel with breakfast. Desert safari with BBQ dinner. Marina dhow cruise dinner. "
                "Burj Khalifa 124th floor. Miracle Garden visit. " * 3,
        "offers": [{
            "seller": "Vague Tours", "seller_type": "DMC", "package_name": "Dubai Holiday", "hotel_star": 4, "nights": 4, "days": 5,
            "destination": "Dubai", "price_from": 450, "currency": "USD", "price_basis": "per person", "flights_included": False,
            "components": [{"name": n, "status": "yes", "evidence": e} for n, e in [
                ("4-star hotel (4 nights)", "Hotel with breakfast"), ("Daily breakfast", "Hotel with breakfast"),
                ("Desert safari with BBQ dinner", "Desert safari with BBQ dinner"),
                ("Marina dhow cruise dinner", "Marina dhow cruise dinner"),
                ("Burj Khalifa 124th floor", "Burj Khalifa 124th floor"), ("Miracle Garden visit", "Miracle Garden visit")]]}],
    },
    # listing page: no offers itself, links to a child page that is a full match
    "https://list.example.com/dubai-packages": {"text": "Dubai packages listing. " * 30, "offers": [],
        "links": [("https://list.example.com/dubai-4-nights-5-days-package", "Dubai 4 Nights 5 Days package"),
                  ("https://list.example.com/blog/x", "blog")]},
    "https://list.example.com/dubai-4-nights-5-days-package": {
        "text": "Dubai 5 Days 4 Nights package. USD 480 per person. 4 star hotel for 4 nights. Daily breakfast included. "
                "Desert safari with BBQ dinner. Marina dhow cruise dinner. Burj Khalifa 124th floor entry. Miracle Garden visit. "
                "Transfers on sharing basis. " * 3,
        "offers": [{
            "seller": "Listing Tours", "seller_type": "Tour operator", "package_name": "Dubai 4N/5D", "hotel_star": 4, "nights": 4,
            "days": 5, "destination": "Dubai", "price_from": 480, "currency": "USD", "price_basis": "per person",
            "flights_included": False, "transfers": "SIC",
            "components": [{"name": n, "status": "yes", "evidence": e} for n, e in [
                ("4-star hotel (4 nights)", "4 star hotel for 4 nights"), ("Daily breakfast", "Daily breakfast included"),
                ("Desert safari with BBQ dinner", "Desert safari with BBQ dinner"),
                ("Marina dhow cruise dinner", "Marina dhow cruise dinner"),
                ("Burj Khalifa 124th floor", "Burj Khalifa 124th floor entry"), ("Miracle Garden visit", "Miracle Garden visit")]]}],
    },
}
for _p in PAGES.values():
    for _o in _p["offers"]:
        _o.setdefault("offer_kind", "multi_day_package")
        _o.setdefault("price_scope", "entire_package")
BLOCKED = {"https://blocked.example.com/dubai": "HTTP 403"}


class FakeLLM:
    def __init__(self):
        self.calls = []

    def vision_json(self, slices, system, prompt):
        self.calls.append(("vision", len(slices)))
        return json.loads(json.dumps(REF_JSON))

    def text_json(self, system, prompt, max_tokens=4000):
        if "diverse Google-style queries" in prompt:
            return {"queries": ["dubai 5 days 4 nights package", "dubai desert safari dhow cruise burj khalifa package"]}
        for url, page in PAGES.items():
            if f"PAGE URL: {url}" in prompt:
                return {"offers": page["offers"]}
        return {"offers": []}


class FakeEngine:
    supports_country = True

    def __init__(self, name="fake", fail=False):
        self.name = name
        self.fail = fail

    def search(self, query, country=None):
        if self.fail:
            raise UpstreamError("boom")
        urls = [u for u in PAGES if "list.example.com/dubai-4" not in u] + list(BLOCKED) + [
            "https://www.ubidtours.com/package/own", "https://www.facebook.com/x", "https://example.com/brochure.pdf",
            "https://exact.example.com/dubai-5d4n?utm_source=x",
        ]
        return [SearchHit(url=u, title="t", query=query, engine=self.name) for u in urls]


class FakeFetcher:
    def fetch(self, url):
        if url in PAGES:
            return FetchResult(True, url, text=PAGES[url]["text"], status=200, links=PAGES[url].get("links", []))
        return FetchResult(False, url, error=BLOCKED.get(url, "HTTP 404"), status=404)


@pytest.fixture
def settings(tmp_path):
    return Settings(openai_api_key="test", db_path=str(tmp_path / "t.db"), live_fx=False,
                    own_domains=["ubidtours.com"], verify_workers=3, app_password="secret")


@pytest.fixture
def fx():
    return CurrencyService(None, live=False)


@pytest.fixture
def app_service(settings, fx):
    db = Database(settings.db_file)
    return AppService(settings, db, FakeLLM(), MultiSearch([FakeEngine()]), FakeFetcher(), fx)


def make_png(w=900, h=3000):
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    for y in range(0, h, 60):
        d.text((20, y), f"Dubai package line {y}", fill="black")
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def wait_for(app, aid, statuses, timeout=40):
    end = time.time() + timeout
    a = None
    while time.time() < end:
        a = app.get(aid)
        if a["status"] in statuses:
            return a
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {statuses}; last={a and (a['status'], a['message'])}")
