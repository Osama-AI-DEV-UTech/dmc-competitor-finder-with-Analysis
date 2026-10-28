"""Currency conversion. Defaults are approximate and clearly labelled; live rates (free API) override when reachable."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..db import Database

log = logging.getLogger(__name__)

# USD per 1 unit. Approximate fallbacks, NOT live. The Excel export labels the source of each rate used.
DEFAULT_RATES: dict[str, float] = {
    "USD": 1.0, "AED": 0.2723, "PKR": 0.003567, "INR": 0.0114, "EUR": 1.10, "GBP": 1.30,
    "SGD": 0.77, "MYR": 0.24, "SAR": 0.2667, "QAR": 0.2747, "OMR": 2.60, "KWD": 3.25, "BHD": 2.65,
    "THB": 0.029, "IDR": 0.000061, "LKR": 0.0033, "BDT": 0.0082, "NPR": 0.0071, "TRY": 0.023,
    "EGP": 0.02, "AUD": 0.66, "NZD": 0.60, "CAD": 0.72, "JPY": 0.0067, "CNY": 0.14, "HKD": 0.128,
    "KRW": 0.00072, "VND": 0.00004, "PHP": 0.0175, "CHF": 1.15, "ZAR": 0.055, "MVR": 0.065,
}

LIVE_URL = "https://open.er-api.com/v6/latest/USD"
MAX_AGE = timedelta(hours=24)


class CurrencyService:
    def __init__(self, db: Optional[Database] = None, live: bool = True, timeout: int = 8):
        self.db = db
        self.live = live
        self.timeout = timeout
        self._rates: dict[str, tuple[float, str]] = {c: (r, "default (approximate, not live)") for c, r in DEFAULT_RATES.items()}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        cached = self.db.load_fx() if self.db else {}
        fresh = False
        for cur, row in cached.items():
            self._rates[cur] = (row["usd_per_unit"], f"{row['source']} ({row['fetched_at'][:10]})")
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
                fresh = age < MAX_AGE
            except ValueError:
                pass
        if self.live and not fresh:
            self._fetch_live()

    def _fetch_live(self) -> None:
        try:
            r = httpx.get(LIVE_URL, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            rates = data.get("rates") or {}
            if data.get("result") != "success" or not rates:
                raise ValueError("unexpected FX payload")
            stamp = datetime.now(timezone.utc).date().isoformat()
            to_save: dict[str, tuple[float, str]] = {}
            for cur, per_usd in rates.items():
                if isinstance(per_usd, (int, float)) and per_usd > 0 and cur in DEFAULT_RATES:
                    usd_per = 1.0 / per_usd
                    self._rates[cur] = (usd_per, f"open.er-api.com live ({stamp})")
                    to_save[cur] = (usd_per, "open.er-api.com live")
            if self.db and to_save:
                self.db.save_fx(to_save)
            log.info("Live FX loaded for %d currencies", len(to_save))
        except Exception as e:  # network down, API change, etc. -> keep defaults
            log.warning("Live FX unavailable (%s); using defaults/cached rates", e)

    def rate(self, currency: str) -> Optional[float]:
        self.load()
        item = self._rates.get((currency or "").upper())
        return item[0] if item else None

    def source(self, currency: str) -> str:
        self.load()
        item = self._rates.get((currency or "").upper())
        return item[1] if item else "unknown currency"

    def to_usd(self, amount: Optional[float], currency: str) -> Optional[float]:
        if amount is None:
            return None
        r = self.rate(currency)
        if r is None:
            return None
        return round(amount * r, 2)

    def table(self, currencies: set[str]) -> list[tuple[str, float, str]]:
        self.load()
        out = []
        for c in sorted(currencies | {"USD"}):
            item = self._rates.get(c)
            if item:
                out.append((c, item[0], item[1]))
        return out
