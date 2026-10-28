"""Download a page and turn it into clean text plus candidate package links. Falls back to Jina Reader / Playwright."""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
MAX_BYTES = 4_000_000
MAX_TEXT = 45_000
MIN_USEFUL = 400


@dataclass
class FetchResult:
    ok: bool
    url: str
    text: str = ""
    error: str = ""
    status: Optional[int] = None
    via: str = "direct"  # direct | jina | render
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, anchor text)


def html_to_text(html: str, base_url: str = "") -> tuple[str, list[tuple[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")
    hints: list[str] = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        raw = (tag.string or tag.get_text() or "").strip()
        if raw and len(raw) < 20000:
            try:
                hints.append("JSON-LD: " + json.dumps(json.loads(raw), ensure_ascii=False)[:3000])
            except json.JSONDecodeError:
                pass
    for m in soup.find_all("meta"):
        key = m.get("property") or m.get("name") or m.get("itemprop")
        if key in {"og:title", "og:description", "description", "product:price:amount", "product:price:currency", "price", "priceCurrency"}:
            val = m.get("content")
            if val:
                hints.append(f"META {key}: {val}")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absu = urljoin(base_url, href) if base_url else href
        absu = absu.split("#")[0]
        if absu.startswith(("http://", "https://")) and absu not in seen:
            seen.add(absu)
            links.append((absu, " ".join(a.get_text(" ", strip=True).split())[:120]))
        if len(links) >= 400:
            break
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav", "form"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    out = ("\n".join(hints) + "\n\n" + text) if hints else text
    return out[:MAX_TEXT], links


class Fetcher:
    def __init__(self, timeout: int = 20, render: bool = False, jina: bool = True):
        self.timeout = timeout
        self.render = render
        self.jina = jina
        self._cache: dict[str, FetchResult] = {}
        self._lock = threading.Lock()

    def fetch(self, url: str) -> FetchResult:
        with self._lock:
            hit = self._cache.get(url)
        if hit is not None:
            return hit
        res = self._fetch(url)
        with self._lock:
            if len(self._cache) > 600:
                self._cache.clear()
            self._cache[url] = res
        return res

    # ------------------------------------------------------------------ strategies
    def _fetch(self, url: str) -> FetchResult:
        direct = self._direct(url)
        if direct.ok and len(direct.text) >= MIN_USEFUL:
            return direct
        for name, fn in (("render", self._render if self.render else None), ("jina", self._jina if self.jina else None)):
            if fn is None:
                continue
            alt = fn(url)
            if alt.ok and len(alt.text) >= MIN_USEFUL:
                if direct.links and not alt.links:
                    alt.links = direct.links
                return alt
        if direct.ok:
            direct.ok = False
            direct.error = "Page has almost no readable text (JavaScript-rendered or blocked)."
        return direct

    def _direct(self, url: str) -> FetchResult:
        try:
            with httpx.Client(follow_redirects=True, timeout=self.timeout, headers={
                "User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9",
            }) as client:
                with client.stream("GET", url) as r:
                    status = r.status_code
                    ctype = r.headers.get("content-type", "").lower()
                    if status >= 400:
                        return FetchResult(False, url, error=f"HTTP {status}", status=status)
                    if ctype and "html" not in ctype and "text" not in ctype:
                        return FetchResult(False, url, error=f"Unsupported content type: {ctype.split(';')[0]}", status=status)
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        chunks.append(chunk)
                        if size > MAX_BYTES:
                            break
                    html = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
                    final = str(r.url)
        except httpx.TimeoutException:
            return FetchResult(False, url, error="Timed out")
        except httpx.HTTPError as e:
            return FetchResult(False, url, error=f"Network error: {type(e).__name__}")
        except Exception as e:  # never let one bad page kill the run
            log.exception("unexpected fetch error for %s", url)
            return FetchResult(False, url, error=f"Unexpected error: {type(e).__name__}")
        text, links = html_to_text(html, final)
        return FetchResult(True, final, text=text, status=status, links=links)

    def _jina(self, url: str) -> FetchResult:
        """Jina Reader renders JS pages and bypasses many simple bot blocks (free tier, no key)."""
        try:
            r = httpx.get(f"https://r.jina.ai/{url}", timeout=self.timeout + 15,
                          headers={"Accept": "text/plain", "User-Agent": UA})
            if r.status_code >= 400:
                return FetchResult(False, url, error=f"Reader HTTP {r.status_code}", via="jina")
            text = re.sub(r"\n{3,}", "\n\n", r.text).strip()[:MAX_TEXT]
            return FetchResult(True, url, text=text, status=200, via="jina")
        except httpx.HTTPError as e:
            return FetchResult(False, url, error=f"Reader network error: {type(e).__name__}", via="jina")

    def _render(self, url: str) -> FetchResult:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent=UA)
                    page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
                    html, final = page.content(), page.url
                finally:
                    browser.close()
            text, links = html_to_text(html, final)
            return FetchResult(True, final, text=text, links=links, via="render")
        except Exception as e:
            log.warning("render failed for %s: %s", url, e)
            return FetchResult(False, url, error=f"Render failed: {type(e).__name__}", via="render")


def package_links(result: FetchResult, destination_tokens: set[str], nights: Optional[int], days: Optional[int],
                  signature: set[str], limit: int = 12) -> list[tuple[str, int]]:
    """Rank same-site links on a listing page that look like individual package pages."""
    base = (urlparse(result.url).hostname or "").lower().removeprefix("www.")
    bad = ("blog", "privacy", "terms", "contact", "login", "signup", "cart", "about", "career", "faq", "policy",
           "sitemap", "javascript", "facebook", "instagram", "whatsapp", "visa", "flight", "hotel-booking")
    scored: list[tuple[str, int]] = []
    for url, anchor in result.links:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if host != base or url.rstrip("/") == result.url.rstrip("/"):
            continue
        blob = f"{url} {anchor}".lower()
        if any(b in blob for b in bad):
            continue
        if not any(t in blob for t in ("package", "tour", "holiday", "trip", "itinerar", "vacation")):
            continue
        score = 0
        score += 3 if any(t in blob for t in destination_tokens) else 0
        if nights and re.search(rf"(?<!\d){nights}\s*[- ]?(n\b|night)", blob):
            score += 4
        if days and re.search(rf"(?<!\d){days}\s*[- ]?(d\b|day)", blob):
            score += 3
        score += sum(1 for t in signature if t in blob)
        if score >= 3:
            scored.append((url, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]
