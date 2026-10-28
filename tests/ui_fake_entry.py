"""Entry script for the browser test: real Streamlit UI, fake LLM/search/fetcher."""
import os
import runpy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_PASSWORD", "letmein")
os.environ.setdefault("OPENAI_API_KEY", "sk-fake")
os.environ.setdefault("LIVE_FX", "false")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "dmc_ui_test", "t.db"))
os.environ.setdefault("OWN_DOMAINS", "ubidtours.com")

import app.bootstrap as bootstrap  # noqa: E402

if bootstrap._override is None:
    from app.config import get_settings
    from app.core import AppService
    from app.db import Database
    from app.services.currency import CurrencyService
    from app.services.search_providers import MultiSearch
    from tests.conftest import FakeEngine, FakeFetcher, FakeLLM

    s = get_settings()
    bootstrap._override = AppService(s, Database(s.db_file), FakeLLM(), MultiSearch([FakeEngine()]), FakeFetcher(),
                                     CurrencyService(None, live=False))

runpy.run_path(str(ROOT / "streamlit_app.py"), run_name="__main__")
