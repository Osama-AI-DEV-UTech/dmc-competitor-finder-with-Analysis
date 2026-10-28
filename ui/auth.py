"""Shared-password gate with throttling. Refuses to run unprotected unless APP_AUTH_DISABLED=true."""
from __future__ import annotations

import hashlib
import hmac
import threading
import time

import streamlit as st

from app.config import Settings

MAX_FAILS = 5
LOCK_SECONDS = 60
GLOBAL_FAILS = 30
GLOBAL_WINDOW = 300

_lock = threading.Lock()
_global_fails: list[float] = []


def check_password(settings: Settings, attempt: str) -> bool:
    attempt = attempt or ""
    if settings.app_password_sha256:
        digest = hashlib.sha256(attempt.encode()).hexdigest()
        return hmac.compare_digest(digest, settings.app_password_sha256)
    if settings.app_password:
        return hmac.compare_digest(attempt.encode(), settings.app_password.encode())
    return False


def _global_locked() -> bool:
    now = time.time()
    with _lock:
        _global_fails[:] = [t for t in _global_fails if now - t < GLOBAL_WINDOW]
        return len(_global_fails) >= GLOBAL_FAILS


def require_login(settings: Settings) -> bool:
    """Render the login form when needed. Returns True when the user may see the app."""
    if settings.auth_disabled:
        return True
    if not (settings.app_password or settings.app_password_sha256):
        st.error("Login is not configured. Set APP_PASSWORD (or APP_PASSWORD_SHA256) in .env / Streamlit secrets, "
                 "or set APP_AUTH_DISABLED=true to run without a password (not recommended).")
        return False
    if st.session_state.get("authed"):
        return True
    st.markdown("## 🔒 DMC Competitor Finder")
    st.caption("Enter the team password to continue.")
    fails = st.session_state.get("fails", 0)
    until = st.session_state.get("locked_until", 0.0)
    with st.form("login", clear_on_submit=True):
        pw = st.text_input("Password", type="password")
        go = st.form_submit_button("Sign in", type="primary")
    if go:
        now = time.time()
        if now < until or _global_locked():
            st.error("Too many failed attempts. Wait a minute and try again.")
        elif check_password(settings, pw):
            st.session_state["authed"] = True
            st.session_state["fails"] = 0
            st.rerun()
        else:
            fails += 1
            with _lock:
                _global_fails.append(now)
            st.session_state["fails"] = fails
            if fails >= MAX_FAILS:
                st.session_state["locked_until"] = now + LOCK_SECONDS
                st.session_state["fails"] = 0
            time.sleep(1.0)  # slow down guessing
            st.error("Wrong password.")
    return False


def logout_button() -> None:
    if st.session_state.get("authed") and st.sidebar.button("Log out"):
        st.session_state.clear()
        st.rerun()
