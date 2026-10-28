"""End-to-end orchestration: extract -> (review) -> search -> verify -> match -> analytics."""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..db import Database
from ..errors import AppError, Conflict, ExtractionError, ValidationFailed
from ..models import AnalysisOptions, Offer, ReferencePackage
from .analytics import compute_analytics
from .currency import CurrencyService
from .extractor import extract_reference
from .fetcher import Fetcher, package_links
from .llm import LLM, SearchHit
from .matcher import LEVEL_ORDER, dedupe_offers, score_offer
from .search_providers import MultiSearch
from .searcher import build_queries, discover, domain_of, select_candidates, signature_items
from .verifier import verify_candidate

log = logging.getLogger(__name__)


PRESETS = {
    # queries, candidate pages, expansion pages, countries used for regional variants, queries that get regional variants
    "fast": dict(queries=8, candidates=25, expand=10, countries=0, regional_queries=2),
    "standard": dict(queries=12, candidates=45, expand=25, countries=3, regional_queries=3),
    "deep": dict(queries=20, candidates=80, expand=50, countries=6, regional_queries=4),
}


class Cancelled(AppError):
    status_code = 409
    code = "cancelled"


@dataclass
class Services:
    settings: Settings
    db: Database
    llm: LLM
    search: MultiSearch
    fetcher: Fetcher
    fx: CurrencyService
    cancel_flags: set = field(default_factory=set)
    running: set = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check_cancel(self, aid: str) -> None:
        if aid in self.cancel_flags:
            raise Cancelled("Cancelled by user")


def _progress(svc: Services, aid: str, status: str, stage: str, pct: int, msg: str) -> None:
    svc.db.update_analysis(aid, status=status, stage=stage, progress=pct, message=msg)
    svc.db.add_event(aid, msg)
    log.info("[%s] %s", aid, msg)


def _load_options(a: dict) -> AnalysisOptions:
    raw = a.get("options_json")
    return AnalysisOptions(**json.loads(raw)) if raw else AnalysisOptions()


def load_reference(a: dict) -> Optional[ReferencePackage]:
    raw = a.get("reference_json")
    return ReferencePackage(**json.loads(raw)) if raw else None


# ------------------------------------------------------------------ stage 1
def run_extraction(svc: Services, aid: str) -> None:
    """Read the screenshot. Leaves the analysis 'awaiting_review' or continues straight into the search."""
    try:
        a = svc.db.get_analysis(aid)
        opts = _load_options(a)
        _progress(svc, aid, "extracting", "extract", 8, "Reading the screenshot and extracting package details…")
        path = Path(a["image_path"])
        if not path.exists():
            raise ExtractionError("The uploaded screenshot file is missing on the server. Please upload it again.")
        ref = extract_reference(svc.llm, path.read_bytes())
        svc.db.update_analysis(aid, reference_json=ref.model_dump_json())
        svc.check_cancel(aid)
        n = len(ref.components)
        if opts.review_before_search:
            _progress(svc, aid, "awaiting_review", "review", 20,
                      f"Extracted '{ref.title or ref.destination}' with {n} components. Review it, then start the search.")
            return
        _progress(svc, aid, "queued", "extract", 22, f"Extracted {n} components. Starting search…")
    except Cancelled as e:
        _fail(svc, aid, e.message)
        return
    except AppError as e:
        _fail(svc, aid, e.message, e.details)
        return
    except Exception as e:
        log.exception("extraction crashed")
        _fail(svc, aid, f"Unexpected error while reading the screenshot: {type(e).__name__}")
        return
    run_search(svc, aid)


# ------------------------------------------------------------------ stage 2
def run_search(svc: Services, aid: str) -> None:
    s = svc.settings
    try:
        a = svc.db.get_analysis(aid)
        ref = load_reference(a)
        if ref is None:
            raise ValidationFailed("No reference package yet; extract it first.")
        opts = _load_options(a)
        preset = PRESETS[opts.depth]
        max_q = opts.max_queries or preset["queries"]
        max_c = opts.max_candidates or preset["candidates"]
        countries = s.search_countries[: preset["countries"]]

        _progress(svc, aid, "searching", "search", 25,
                  f"Planning search queries… engines: {', '.join(s.search_engines())}")
        queries = build_queries(svc.llm, ref, max_q, opts.extra_queries)
        svc.db.add_event(aid, f"Queries: {json.dumps(queries, ensure_ascii=False)}")
        svc.check_cancel(aid)
        regions = {c: queries[: preset["regional_queries"]] for c in countries
                   if any(e.supports_country for e in svc.search.engines)}

        def on_event(msg: str, level: str = "INFO") -> None:
            svc.db.add_event(aid, msg, level)

        def q_done(i: int, n: int) -> None:
            svc.db.update_analysis(aid, progress=25 + int(20 * i / n), message=f"Searching… {i}/{n} searches done")

        hits = discover(svc.search, queries, regions, on_event, q_done)
        svc.check_cancel(aid)
        cands = select_candidates(hits, s.own_domains, max_c)
        svc.db.add_event(aid, f"{len(hits)} search results -> {len(cands)} candidate pages to verify")
        if not cands:
            summary = compute_analytics(ref, [], svc.fx.to_usd(ref.price_amount, ref.price_currency))
            summary["fx_snapshot"] = {c: [r, src] for c, r, src in svc.fx.table({ref.price_currency})}
            svc.db.replace_offers(aid, [])
            svc.db.update_analysis(aid, summary_json=json.dumps(summary))
            _progress(svc, aid, "completed", "done", 100, "Search finished but no candidate pages were found.")
            return

        offers, links = _verify_all(svc, aid, ref, cands, 46, 78)
        svc.check_cancel(aid)
        if opts.expand_links and preset["expand"]:
            extra = _expansion_candidates(ref, links, cands, s.own_domains, preset["expand"])
            if extra:
                svc.db.add_event(aid, f"Following {len(extra)} package links found on listing pages")
                more, _ = _verify_all(svc, aid, ref, extra, 78, 92, label="Verifying linked pages")
                offers.extend(more)
                svc.check_cancel(aid)

        _progress(svc, aid, "matching", "match", 92, "Scoring matches and building analytics…")
        ref_usd = svc.fx.to_usd(ref.price_amount, ref.price_currency)
        scored = [score_offer(ref, o, svc.fx, opts, ref_usd, s.price_outlier_low, s.price_outlier_high) for o in offers]
        scored = dedupe_offers(scored)
        scored.sort(key=lambda o: (LEVEL_ORDER[o.match_level], not o.in_stats, -o.match_score,
                                   o.price_usd_from if o.price_usd_from is not None else 1e12))
        summary = compute_analytics(ref, scored, ref_usd)
        used = {o.currency for o in scored} | {ref.price_currency}
        summary["fx_snapshot"] = {c: [r, src] for c, r, src in svc.fx.table(used)}
        svc.db.replace_offers(aid, [o.model_dump() for o in scored])
        svc.db.update_analysis(aid, summary_json=json.dumps(summary))
        _progress(svc, aid, "completed", "done", 100,
                  f"Done: {summary['same']} exact + {summary['by_level']['near']} near matches, "
                  f"{summary['wide_count']} whole packages with a verified price, {summary['leads_to_check']} pages need a manual check.")
    except Cancelled as e:
        _fail(svc, aid, e.message)
    except AppError as e:
        _fail(svc, aid, e.message, e.details)
    except Exception as e:
        log.exception("search pipeline crashed")
        _fail(svc, aid, f"Unexpected error: {type(e).__name__}: {e}")
    finally:
        svc.cancel_flags.discard(aid)
        svc.running.discard(aid)


def _expansion_candidates(ref: ReferencePackage, links: list, seen: list[SearchHit], own: list[str], limit: int) -> list[SearchHit]:
    """Package-looking links found on listing pages that we have not verified yet."""
    done = {h.url for h in seen}
    dest = {t for t in re.findall(r"[a-z0-9]+", f"{ref.destination} {' '.join(ref.cities)}".lower()) if len(t) > 2}
    sig = {t for n in signature_items(ref) for t in re.findall(r"[a-z]+", n.lower()) if len(t) > 3}
    ranked: list[tuple[str, int]] = []
    for fr in links:
        ranked.extend(package_links(fr, dest, ref.nights, ref.days, sig))
    ranked.sort(key=lambda x: -x[1])
    own_set = {d.lower() for d in own}
    out: list[SearchHit] = []
    for url, _ in ranked:
        host = domain_of(url)
        if url in done or any(host == d or host.endswith("." + d) for d in own_set):
            continue
        done.add(url)
        out.append(SearchHit(url=url, title="", snippet="", query="link-expansion", engine="expand"))
        if len(out) >= limit:
            break
    return out


def _verify_all(svc: Services, aid: str, ref: ReferencePackage, cands, p0: int, p1: int,
                label: str = "Verifying candidate pages"):
    total = len(cands)
    _progress(svc, aid, "verifying", "verify", p0, f"{label}: {total} pages…")
    offers: list[Offer] = []
    fetched: list = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, svc.settings.verify_workers)) as pool:
        futs = {pool.submit(verify_candidate, svc.llm, svc.fetcher, ref, h): h for h in cands}
        try:
            for fut in as_completed(futs):
                h = futs[fut]
                try:
                    res = fut.result()
                    offers.extend(res.offers)
                    if res.fetched is not None and res.fetched.ok and res.links:
                        res.fetched.links = res.links
                        fetched.append(res.fetched)
                except AppError as e:
                    if e.code == "config_error":
                        raise
                    svc.db.add_event(aid, f"Verify failed for {h.url}: {e.message}", "WARNING")
                except Exception as e:
                    log.exception("verify crashed for %s", h.url)
                    svc.db.add_event(aid, f"Verify crashed for {h.url}: {type(e).__name__}", "WARNING")
                done += 1
                svc.db.update_analysis(aid, progress=p0 + int((p1 - p0) * done / total),
                                       message=f"{label}… {done}/{total} ({len(offers)} offers so far)")
                svc.check_cancel(aid)
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    return offers, fetched


def _fail(svc: Services, aid: str, message: str, details: Optional[str] = None) -> None:
    try:
        svc.db.update_analysis(aid, status="failed", stage="error", message=message, error=message)
        svc.db.add_event(aid, f"FAILED: {message}" + (f" | {details[:300]}" if details else ""), "ERROR")
    except Exception:
        log.exception("could not record failure")
    log.error("[%s] failed: %s", aid, message)
    svc.cancel_flags.discard(aid)
    svc.running.discard(aid)


# ------------------------------------------------------------------ job control
class JobRunner:
    def __init__(self, svc: Services, workers: int = 3):
        self.svc = svc
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")

    def submit(self, aid: str, fn) -> None:
        with self.svc._lock:
            if aid in self.svc.running:
                raise Conflict("This analysis is already running.")
            self.svc.running.add(aid)
        self.svc.cancel_flags.discard(aid)
        self.pool.submit(self._guard, aid, fn)

    def _guard(self, aid: str, fn) -> None:
        try:
            fn(self.svc, aid)
        except Exception:  # fn handles its own errors; this is the last line of defence
            log.exception("job crashed")
            _fail(self.svc, aid, "Internal error while running the analysis.")
        finally:
            self.svc.running.discard(aid)

    def cancel(self, aid: str) -> None:
        if aid in self.svc.running:
            self.svc.cancel_flags.add(aid)
