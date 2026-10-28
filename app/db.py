"""SQLite persistence (stdlib sqlite3). One short-lived connection per call; WAL for concurrency."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .errors import NotFound

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    image_name TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    reference_json TEXT,
    options_json TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    data_json TEXT NOT NULL,
    UNIQUE(analysis_id, url, data_json)
);
CREATE INDEX IF NOT EXISTS idx_offers_analysis ON offers(analysis_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_analysis ON events(analysis_id);
CREATE TABLE IF NOT EXISTS fx_rates (
    currency TEXT PRIMARY KEY,
    usd_per_unit REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._write_lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as c:
            try:
                c.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass
            c.executescript(SCHEMA)

    # ---------------------------------------------------------------- analyses
    def create_analysis(self, image_name: str, image_path: str) -> str:
        aid = uuid.uuid4().hex[:12]
        ts = now_iso()
        with self._write_lock, self._conn() as c:
            c.execute(
                "INSERT INTO analyses (id, created_at, updated_at, status, stage, progress, message, image_name, image_path)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (aid, ts, ts, "uploaded", "upload", 2, "Screenshot uploaded", image_name, image_path),
            )
        return aid

    def update_analysis(self, aid: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "stage", "progress", "message", "error", "reference_json", "options_json", "summary_json"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unknown analysis fields: {bad}")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._write_lock, self._conn() as c:
            cur = c.execute(f"UPDATE analyses SET {cols} WHERE id=?", (*fields.values(), aid))
            if cur.rowcount == 0:
                raise NotFound(f"Analysis {aid} not found")

    def get_analysis(self, aid: str) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()
        if not row:
            raise NotFound(f"Analysis {aid} not found")
        return dict(row)

    def list_analyses(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT a.*, (SELECT COUNT(*) FROM offers o WHERE o.analysis_id=a.id) AS offers_count"
                " FROM analyses a ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_analysis(self, aid: str) -> Optional[str]:
        a = self.get_analysis(aid)
        with self._write_lock, self._conn() as c:
            c.execute("DELETE FROM analyses WHERE id=?", (aid,))
        return a.get("image_path") or None

    # ------------------------------------------------------------------ offers
    def replace_offers(self, aid: str, offers: list[dict[str, Any]]) -> None:
        with self._write_lock, self._conn() as c:
            c.execute("DELETE FROM offers WHERE analysis_id=?", (aid,))
            for o in offers:
                c.execute(
                    "INSERT OR IGNORE INTO offers (analysis_id, url, data_json) VALUES (?,?,?)",
                    (aid, o.get("url", ""), json.dumps(o, ensure_ascii=False)),
                )

    def get_offers(self, aid: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT data_json FROM offers WHERE analysis_id=? ORDER BY id", (aid,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # ------------------------------------------------------------------ events
    def add_event(self, aid: str, message: str, level: str = "INFO") -> None:
        try:
            with self._write_lock, self._conn() as c:
                c.execute(
                    "INSERT INTO events (analysis_id, ts, level, message) VALUES (?,?,?,?)",
                    (aid, now_iso(), level, message[:2000]),
                )
        except sqlite3.Error:
            log.exception("could not write event")

    def get_events(self, aid: str, limit: int = 300) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, level, message FROM events WHERE analysis_id=? ORDER BY id DESC LIMIT ?", (aid, limit)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---------------------------------------------------------------- fx rates
    def save_fx(self, rates: dict[str, tuple[float, str]]) -> None:
        ts = now_iso()
        with self._write_lock, self._conn() as c:
            for cur, (rate, source) in rates.items():
                c.execute(
                    "INSERT INTO fx_rates (currency, usd_per_unit, source, fetched_at) VALUES (?,?,?,?)"
                    " ON CONFLICT(currency) DO UPDATE SET usd_per_unit=excluded.usd_per_unit,"
                    " source=excluded.source, fetched_at=excluded.fetched_at",
                    (cur, rate, source, ts),
                )

    def load_fx(self) -> dict[str, dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT currency, usd_per_unit, source, fetched_at FROM fx_rates").fetchall()
        return {r["currency"]: dict(r) for r in rows}

    def recover_interrupted(self) -> int:
        """Mark analyses left mid-run by a crashed/stopped server as failed so the UI doesn't spin forever."""
        busy = ("extracting", "queued", "searching", "verifying", "matching")
        with self._write_lock, self._conn() as c:
            cur = c.execute(
                f"UPDATE analyses SET status='failed', error='Server restarted while this analysis was running. Please re-run.',"
                f" updated_at=? WHERE status IN ({','.join('?' * len(busy))})",
                (now_iso(), *busy),
            )
            return cur.rowcount
