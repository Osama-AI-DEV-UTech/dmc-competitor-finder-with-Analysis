"""All web-search engines. Every enabled engine is queried for every query and the results are merged."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..config import Settings
from ..errors import AppError, ConfigError, UpstreamError
from .llm import OpenAILLM, SearchHit

log = logging.getLogger(__name__)


class Engine(Protocol):
    name: str
    supports_country: bool

    def search(self, query: str, country: Optional[str] = None) -> list[SearchHit]: ...


class OpenAIEngine:
    name = "openai"
    supports_country = True

    def __init__(self, llm: OpenAILLM):
        self.llm = llm

    def search(self, query: str, country: Optional[str] = None) -> list[SearchHit]:
        return self.llm.web_search(query, country)


class DuckDuckGoEngine:
    name = "duckduckgo"
    supports_country = False

    def search(self, query: str, country: Optional[str] = None) -> list[SearchHit]:
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # older package name
        except ImportError:
            raise UpstreamError("duckduckgo: package 'ddgs' is not installed (pip install ddgs).")
        try:
            rows = DDGS().text(query, max_results=15) or []
        except Exception as e:  # the library raises many different exception types
            raise UpstreamError(f"duckduckgo: {type(e).__name__}: {str(e)[:120]}")
        return [SearchHit(url=r.get("href") or r.get("url") or "", title=r.get("title", ""), snippet=(r.get("body") or "")[:400],
                          query=query, engine=self.name) for r in rows if (r.get("href") or r.get("url"))]


@dataclass
class SearchReport:
    hits: list[SearchHit] = field(default_factory=list)
    per_engine: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class MultiSearch:
    """Runs one query on every engine in parallel; tolerates individual engine failures."""

    def __init__(self, engines: list[Engine]):
        if not engines:
            raise ConfigError("No web-search engine is enabled.")
        self.engines = engines

    def search_detailed(self, query: str, country: Optional[str] = None) -> SearchReport:
        engines = [e for e in self.engines if (e.supports_country or country is None)]
        rep = SearchReport()

        def run(e: Engine):
            return e, e.search(query, country)

        with ThreadPoolExecutor(max_workers=len(engines) or 1) as pool:
            futs = [(e, pool.submit(run, e)) for e in engines]
            for e, f in futs:
                try:
                    _, hits = f.result()
                    rep.hits.extend(hits)
                    rep.per_engine[e.name] = len(hits)
                except ConfigError:
                    raise  # e.g. OpenAI key/quota problem: no point continuing
                except AppError as ex:
                    rep.errors[e.name] = ex.message
                except Exception as ex:
                    log.exception("engine %s crashed", e.name)
                    rep.errors[e.name] = f"{type(ex).__name__}: {ex}"
        if not rep.hits and rep.errors and len(rep.errors) == len(engines):
            raise UpstreamError("All search engines failed: " + "; ".join(f"{k}: {v}" for k, v in rep.errors.items()))
        return rep

    def search(self, query: str, country: Optional[str] = None) -> list[SearchHit]:
        return self.search_detailed(query, country).hits


def build_search(settings: Settings, llm: Optional[OpenAILLM]) -> MultiSearch:
    engines: list[Engine] = []
    if settings.enable_openai_search and llm is not None:
        engines.append(OpenAIEngine(llm))
    if settings.enable_ddg:
        engines.append(DuckDuckGoEngine())
    return MultiSearch(engines)
