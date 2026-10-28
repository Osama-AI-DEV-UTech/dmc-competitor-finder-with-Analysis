"""Query generation, parallel discovery and candidate-URL filtering."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..errors import AppError, UpstreamError
from ..models import ReferencePackage
from .llm import LLM, SearchHit
from .search_providers import MultiSearch

log = logging.getLogger(__name__)

BLOCKED_DOMAINS = {
    "facebook.com", "instagram.com", "youtube.com", "youtu.be", "tiktok.com", "twitter.com", "x.com",
    "pinterest.com", "linkedin.com", "wikipedia.org", "wikitravel.org", "maps.google.com", "google.com",
    "reddit.com", "quora.com", "medium.com", "blogspot.com", "wordpress.com",
}
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx")
TRACKING = {"fbclid", "gclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src"}

QUERY_SYSTEM = ("You design web-search queries to find competitors who sell the SAME travel package as a reference "
                "package. Return JSON only.")


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
         if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(q), ""))


def _domain_matches(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def signature_items(ref: ReferencePackage, n: int = 5) -> list[str]:
    names = [c.name for c in ref.components if c.category in ("tour", "attraction", "activity")]
    return names[:n]


def fallback_queries(ref: ReferencePackage) -> list[str]:
    dest = ref.destination or ref.title
    if len(ref.cities) > 1:  # multi-city route: "Bali Kuala Lumpur" matches how sellers title such packages
        dest = " ".join(dict.fromkeys([*ref.cities]))
    n = ref.nights
    d = ref.days or (n + 1 if n else None)
    star = f"{ref.hotel_star} star " if ref.hotel_star else ""
    dur = f"{n} nights {d} days" if n and d else (f"{d} days" if d else "")
    sig = signature_items(ref)
    feat = " ".join(sig[:4])
    qs = [
        f"{dest} {dur} package {star}hotel {feat} price",
        f"{dest} {n}N {d}D tour package {feat}",
        f"{dest} {dur} holiday package price per person",
        f"{dest} inbound DMC {dur} package {feat}",
        f"{dest} {dur} land package {star}hotel breakfast airport transfers sightseeing",
        f"{dest} {n} nights {d} days itinerary inclusions {' '.join(sig[:2])}",
        f"{dest} {dur} tour package India price",
        f"{dest} {dur} tour package Pakistan price",
        f"{dest} {dur} tour package UK price",
        f"{dest} {dur} tour package Singapore Malaysia price",
        f"book {dest} {dur} {star}package all inclusive sightseeing {' '.join(sig[2:5])}",
        f"{dest} {n}N{d}D B2B DMC package rates {star}",
    ]
    return [re.sub(r"\s+", " ", q).strip() for q in qs]


def build_queries(llm: Optional[LLM], ref: ReferencePackage, max_queries: int, extra: list[str]) -> list[str]:
    queries: list[str] = []
    if llm is not None:
        try:
            summary = ref.model_dump(include={"title", "destination", "country", "nights", "days", "hotel_star",
                                              "transfers", "components", "operator"})
            data = llm.text_json(
                QUERY_SYSTEM,
                f"Reference package:\n{summary}\n\nWrite {max_queries} diverse Google-style queries (max 14 words each) "
                "that would surface pages of OTHER companies (DMCs, tour operators, OTAs, marketplaces, regional agents) "
                "selling this same package: same destination, same length, same star class and the same signature "
                "attractions/activities. Vary the angle: exact signature items, 'N nights N days package price', "
                "inbound DMC/B2B, and regional markets (India, Pakistan, UAE/Gulf, UK, Singapore/Malaysia). "
                'Return {"queries": [str]}.',
                max_tokens=1500,
            )
            raw = data.get("queries")
            if isinstance(raw, list):
                queries = [re.sub(r"\s+", " ", str(q)).strip() for q in raw if str(q).strip()]
        except AppError as e:
            log.warning("LLM query generation failed (%s); using template queries", e.message)
        except Exception:
            log.exception("LLM query generation crashed; using template queries")
    for q in fallback_queries(ref):
        if len(queries) >= max_queries:
            break
        queries.append(q)
    out, seen = [], set()
    for q in [*extra, *queries]:
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            out.append(q)
    return out[: max_queries + len(extra)]


def discover(search: MultiSearch, queries: list[str], regions: dict[str, list[str]],
             on_event: Callable[[str, str], None], on_done: Optional[Callable[[int, int], None]] = None,
             workers: int = 4) -> list[SearchHit]:
    """Run every (query, country) task on every engine. `regions` maps a country code to the queries that get a regional variant."""
    tasks: list[tuple[str, Optional[str]]] = [(q, None) for q in queries]
    for country, qs in regions.items():
        tasks.extend((q, country) for q in qs)
    hits: list[SearchHit] = []
    failures, done = 0, 0
    totals: dict[str, int] = {}

    def one(t: tuple[str, Optional[str]]):
        return search.search_detailed(t[0], t[1])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, t): t for t in tasks}
        for fut, (q, c) in futures.items():
            try:
                rep = fut.result()
                hits.extend(rep.hits)
                for k, v in rep.per_engine.items():
                    totals[k] = totals.get(k, 0) + v
                for k, msg in rep.errors.items():
                    on_event(f"[{k}] '{q}'{' (' + c + ')' if c else ''}: {msg}", "WARNING")
            except AppError as e:
                failures += 1
                on_event(f"Search failed for '{q}': {e.message}", "WARNING")
                if e.code == "config_error":
                    for f in futures:
                        f.cancel()
                    raise
            except Exception as e:
                failures += 1
                on_event(f"Search failed for '{q}': {type(e).__name__}: {e}", "WARNING")
            done += 1
            if on_done:
                on_done(done, len(tasks))
    if tasks and failures == len(tasks):
        raise UpstreamError("Every search failed. Check the API keys, network and search engine settings.")
    on_event("Results per engine: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())), "INFO")
    return hits


def select_candidates(hits: list[SearchHit], own_domains: list[str], limit: int) -> list[SearchHit]:
    own = {d.lower() for d in own_domains}
    by_url: dict[str, SearchHit] = {}
    votes: dict[str, int] = {}
    for h in hits:
        if not h.url.lower().startswith(("http://", "https://")):
            continue
        host = domain_of(h.url)
        if not host or _domain_matches(host, own) or _domain_matches(host, BLOCKED_DOMAINS):
            continue
        if urlparse(h.url).path.lower().endswith(SKIP_EXT):
            continue
        key = normalize_url(h.url)
        votes[key] = votes.get(key, 0) + 1
        if key not in by_url:
            by_url[key] = SearchHit(url=key, title=h.title, snippet=h.snippet, query=h.query, engine=h.engine)
    ranked = sorted(by_url.values(), key=lambda x: -votes[x.url])
    # keep variety: at most 6 pages per domain so one marketplace cannot crowd out everybody else
    per_domain: dict[str, int] = {}
    out: list[SearchHit] = []
    for h in ranked:
        dom = domain_of(h.url)
        if per_domain.get(dom, 0) >= 6:
            continue
        per_domain[dom] = per_domain.get(dom, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out
