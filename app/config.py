"""Central configuration. Values come from environment variables / .env, or Streamlit secrets when deployed."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _raw(name: str) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and v.strip() != "":
        return v
    try:  # Streamlit Community Cloud / local .streamlit/secrets.toml
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return None


def _str(name: str, default: str = "") -> str:
    v = _raw(name)
    return (v if v is not None else default).split("#")[0].strip()


def _bool(name: str, default: bool) -> bool:
    v = _raw(name)
    if v is None:
        return default
    return v.split("#")[0].strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in _str(name, default).split(",") if x.strip()]


DEFAULT_COUNTRIES = ["IN", "PK", "AE", "GB", "SG"]
MAX_COUNTRIES = 10


def _countries() -> list[str]:
    """Regional search markets. A huge list (e.g. every country) would explode the number of paid searches, so it is ignored."""
    seen: list[str] = []
    for c in _list("SEARCH_COUNTRIES", ",".join(DEFAULT_COUNTRIES)):
        c = c.upper()
        if len(c) == 2 and c.isalpha() and c not in seen:
            seen.append(c)
    return seen if 0 < len(seen) <= MAX_COUNTRIES else list(DEFAULT_COUNTRIES)


@dataclass
class Settings:
    # ---- OpenAI (the only LLM vendor)
    openai_api_key: str = field(default_factory=lambda: _str("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _str("OPENAI_MODEL", "gpt-4.1"))
    openai_search_context: str = field(default_factory=lambda: _str("OPENAI_SEARCH_CONTEXT", "high"))
    # ---- extra web-search engines (all enabled engines are used together)
    enable_openai_search: bool = field(default_factory=lambda: _bool("ENABLE_OPENAI_SEARCH", True))
    enable_ddg: bool = field(default_factory=lambda: _bool("ENABLE_DUCKDUCKGO", True))
    search_countries: list[str] = field(default_factory=lambda: _countries())
    # ---- pipeline
    own_domains: list[str] = field(default_factory=lambda: [d.lower() for d in _list("OWN_DOMAINS", "ubidtours.com,travelomatix.com")])
    fetch_timeout: int = field(default_factory=lambda: _int("FETCH_TIMEOUT_SECONDS", 20))
    verify_workers: int = field(default_factory=lambda: _int("VERIFY_WORKERS", 6))
    fetch_render: bool = field(default_factory=lambda: _bool("FETCH_RENDER", False))
    jina_fallback: bool = field(default_factory=lambda: _bool("JINA_FALLBACK", True))
    live_fx: bool = field(default_factory=lambda: _bool("LIVE_FX", True))
    price_outlier_low: float = field(default_factory=lambda: _float("PRICE_OUTLIER_LOW", 0.35))
    price_outlier_high: float = field(default_factory=lambda: _float("PRICE_OUTLIER_HIGH", 3.0))
    # ---- storage / limits
    db_path: str = field(default_factory=lambda: _str("DB_PATH", "data/dmc_finder.db"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 25))
    # ---- login
    app_password: str = field(default_factory=lambda: _raw("APP_PASSWORD") or "")
    app_password_sha256: str = field(default_factory=lambda: _str("APP_PASSWORD_SHA256").lower())
    auth_disabled: bool = field(default_factory=lambda: _bool("APP_AUTH_DISABLED", False))

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def upload_dir(self) -> Path:
        return self.db_file.parent / "uploads"

    def search_engines(self) -> list[str]:
        out = []
        if self.enable_openai_search and self.openai_api_key:
            out.append("OpenAI web search")
        if self.enable_ddg:
            out.append("DuckDuckGo")
        return out

    def validate_for_run(self) -> list[str]:
        problems: list[str] = []
        if not self.openai_api_key:
            problems.append("OPENAI_API_KEY is not set (needed to read the screenshot and verify pages).")
        if not self.search_engines():
            problems.append("No web-search engine is enabled. Enable OpenAI search or DuckDuckGo, (DuckDuckGo needs no key).")
        return problems


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
