"""Application service: everything the UI needs, in one process (no HTTP API)."""
from __future__ import annotations

import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from PIL import Image, UnidentifiedImageError

from .config import Settings
from .db import Database
from .errors import ConfigError, Conflict, NotFound, ValidationFailed
from .models import AnalysisOptions, Offer, ReferencePackage
from .services.currency import CurrencyService
from .services.exporter import build_workbook, offers_from_rows
from .services.fetcher import Fetcher
from .services.llm import LLM, build_llm
from .services.pipeline import JobRunner, Services, run_extraction, run_search
from .services.search_providers import MultiSearch, build_search

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[0-9a-f]{12}$")
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}


def _check_id(aid: str) -> str:
    if not ID_RE.match(aid or ""):
        raise NotFound("Analysis not found")
    return aid


class AppService:
    def __init__(self, settings: Settings, db: Database, llm: LLM, search: MultiSearch, fetcher: Fetcher, fx: CurrencyService):
        self.settings = settings
        self.db = db
        self.svc = Services(settings=settings, db=db, llm=llm, search=search, fetcher=fetcher, fx=fx)
        self.runner = JobRunner(self.svc, workers=3)
        recovered = db.recover_interrupted()
        if recovered:
            log.warning("Marked %d interrupted analyses as failed", recovered)
        settings.upload_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_settings(cls, settings: Settings) -> "AppService":
        db = Database(settings.db_file)
        try:
            llm = build_llm(settings)
        except ConfigError as e:
            log.warning("LLM not ready: %s", e.message)
            llm = None
        try:
            search = build_search(settings, llm)
        except ConfigError as e:
            log.warning("Search not ready: %s", e.message)
            search = None
        fetcher = Fetcher(settings.fetch_timeout, settings.fetch_render, settings.jina_fallback)
        fx = CurrencyService(db, live=settings.live_fx)
        fx.load()
        return cls(settings, db, llm, search, fetcher, fx)  # type: ignore[arg-type]

    # ------------------------------------------------------------ health
    def problems(self) -> list[str]:
        return self.settings.validate_for_run()

    def _require_ready(self) -> None:
        p = self.problems()
        if p:
            raise ConfigError(" ".join(p))

    def _wait_idle(self, aid: str, seconds: float = 3.0) -> None:
        """A job flips its status a moment before its worker thread is released; wait for that."""
        end = time.time() + seconds
        while aid in self.svc.running and time.time() < end:
            time.sleep(0.05)

    # ------------------------------------------------------------ analyses
    def create_analysis(self, filename: str, data: bytes, options: Optional[AnalysisOptions] = None) -> str:
        self._require_ready()
        if not data:
            raise ValidationFailed("The uploaded file is empty.")
        if len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise ValidationFailed(f"File is larger than {self.settings.max_upload_mb} MB.")
        Image.MAX_IMAGE_PIXELS = 400_000_000
        try:
            with Image.open(io.BytesIO(data)) as im:
                fmt = im.format
                im.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValidationFailed("That file is not a readable image. Upload a PNG, JPG or WEBP screenshot.")
        if fmt not in ALLOWED_FORMATS:
            raise ValidationFailed("Unsupported image type. Upload a PNG, JPG or WEBP screenshot.")
        ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[fmt]
        aid = self.db.create_analysis(Path(filename or "screenshot").name[:120], "")
        path = self.settings.upload_dir / f"{aid}{ext}"
        path.write_bytes(data)
        self.db.update_analysis(aid, options_json=(options or AnalysisOptions()).model_dump_json())
        with self.db._write_lock, self.db._conn() as c:
            c.execute("UPDATE analyses SET image_path=? WHERE id=?", (str(path), aid))
        self.runner.submit(aid, run_extraction)
        return aid

    def list_analyses(self) -> list[dict[str, Any]]:
        return self.db.list_analyses()

    def get(self, aid: str) -> dict[str, Any]:
        a = self.db.get_analysis(_check_id(aid))
        a["reference"] = json.loads(a["reference_json"]) if a.get("reference_json") else None
        a["summary"] = json.loads(a["summary_json"]) if a.get("summary_json") else None
        a["options"] = json.loads(a["options_json"]) if a.get("options_json") else None
        a["running"] = aid in self.svc.running
        return a

    def offers(self, aid: str) -> list[Offer]:
        return offers_from_rows(self.db.get_offers(_check_id(aid)))

    def events(self, aid: str, limit: int = 300) -> list[dict[str, Any]]:
        return self.db.get_events(_check_id(aid), limit)

    def image_bytes(self, aid: str) -> Optional[bytes]:
        a = self.db.get_analysis(_check_id(aid))
        p = Path(a.get("image_path") or "")
        return p.read_bytes() if p.is_file() else None

    def image_preview(self, aid: str, width: int = 700) -> Optional[bytes]:
        raw = self.image_bytes(aid)
        if raw is None:
            return None
        Image.MAX_IMAGE_PIXELS = 400_000_000
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            if im.width > width:
                im = im.resize((width, max(1, int(im.height * width / im.width))))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=80)
            return out.getvalue()

    def update_reference(self, aid: str, ref: ReferencePackage) -> None:
        a = self.db.get_analysis(_check_id(aid))
        if a["status"] not in ("awaiting_review", "completed", "failed", "uploaded"):
            raise Conflict("The analysis is busy; wait until it finishes.")
        if not ref.components:
            raise ValidationFailed("The package needs at least one component.")
        self.db.update_analysis(aid, reference_json=ref.model_dump_json())

    def start(self, aid: str, ref: Optional[ReferencePackage] = None, options: Optional[AnalysisOptions] = None) -> None:
        self._require_ready()
        a = self.db.get_analysis(_check_id(aid))
        if a["status"] in ("awaiting_review", "completed", "failed"):
            self._wait_idle(aid)
        if aid in self.svc.running:
            raise Conflict("This analysis is already running.")
        if ref is not None:
            self.update_reference(aid, ref)
        elif not a.get("reference_json"):
            raise ValidationFailed("The package has not been extracted yet.")
        if options is not None:
            self.db.update_analysis(aid, options_json=options.model_dump_json())
        self.db.update_analysis(aid, status="queued", stage="search", progress=24, message="Queued", error=None)
        self.runner.submit(aid, run_search)

    def retry(self, aid: str) -> None:
        a = self.db.get_analysis(_check_id(aid))
        if a.get("reference_json"):
            self.start(aid)
        else:
            self._require_ready()
            self.db.update_analysis(aid, status="uploaded", error=None, message="Retrying")
            self.runner.submit(aid, run_extraction)

    def cancel(self, aid: str) -> None:
        self.runner.cancel(_check_id(aid))

    def delete(self, aid: str) -> None:
        _check_id(aid)
        if aid in self.svc.running:
            raise Conflict("Cancel the running analysis before deleting it.")
        path = self.db.delete_analysis(aid)
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete %s", path)

    def export_xlsx(self, aid: str) -> bytes:
        a = self.get(aid)
        if a["status"] != "completed" or not a["reference"]:
            raise Conflict("The analysis is not finished yet.")
        ref = ReferencePackage(**a["reference"])
        return build_workbook(a, ref, self.offers(aid), a["summary"] or {})
