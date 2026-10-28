"""One AppService per Streamlit server process (st.cache_resource). Tests can inject a fake via `_override`."""
from __future__ import annotations

from typing import Optional

import streamlit as st

from .config import get_settings
from .core import AppService
from .logging_setup import setup_logging

_override: Optional[AppService] = None


@st.cache_resource(show_spinner=False)
def _build() -> AppService:
    s = get_settings()
    setup_logging(s.log_level)
    return AppService.from_settings(s)


def get_app() -> AppService:
    return _override if _override is not None else _build()
