"""OpenAI access (vision + JSON + built-in web search) behind small interfaces so everything is testable offline."""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from ..config import Settings
from ..errors import ConfigError, ExtractionError, UpstreamError
from .imageprep import Slice

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\")\]]+")


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    query: str = ""
    engine: str = ""


class LLM(Protocol):
    def vision_json(self, slices: list[Slice], system: str, prompt: str) -> dict[str, Any]: ...
    def text_json(self, system: str, prompt: str, max_tokens: int = 4000) -> dict[str, Any]: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply (tolerates code fences and chatter)."""
    if not text:
        raise ExtractionError("The model returned an empty reply.")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", cleaned):
        try:
            obj, _ = dec.raw_decode(cleaned[m.start():])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ExtractionError("The model reply did not contain valid JSON.", details=text[:500])


def clean_openai_url(url: str) -> str:
    url = url.rstrip(".,;")
    return re.sub(r"[?&]utm_source=openai.*$", "", url)


class OpenAILLM:
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ConfigError("OPENAI_API_KEY is not set. Add it to .env (or Streamlit secrets) and restart.")
        import openai  # local import keeps modules importable without the SDK in tests

        self._openai = openai
        self.settings = settings
        self.client = openai.OpenAI(api_key=settings.openai_api_key, timeout=180, max_retries=4)

    # ------------------------------------------------------------------ plumbing
    def _call(self, **kwargs: Any):
        o = self._openai
        try:
            return self.client.responses.create(model=self.settings.openai_model, **kwargs)
        except o.AuthenticationError as e:
            raise ConfigError("OpenAI rejected the API key. Check OPENAI_API_KEY.", details=str(e))
        except o.PermissionDeniedError as e:
            raise ConfigError("This OpenAI key is not allowed to use this model or tool (check project permissions).", details=str(e))
        except o.NotFoundError as e:
            raise ConfigError(f"OpenAI model '{self.settings.openai_model}' not found or not available to your key. "
                              "Change OPENAI_MODEL (for example gpt-4.1 or gpt-4o).", details=str(e))
        except o.RateLimitError as e:
            if "insufficient_quota" in str(e) or "exceeded your current quota" in str(e).lower():
                raise ConfigError("Your OpenAI quota/credit is exhausted. Add billing credit at platform.openai.com.", details=str(e))
            raise UpstreamError("OpenAI rate limit hit even after retries. Try again in a minute.", details=str(e))
        except o.BadRequestError as e:
            msg = str(e).lower()
            if "billing" in msg or "insufficient" in msg:
                raise ConfigError("OpenAI billing problem: " + str(e)[:200], details=str(e))
            raise UpstreamError(f"OpenAI rejected the request: {str(e)[:300]}", details=str(e))
        except (o.APIConnectionError, o.APITimeoutError) as e:
            raise UpstreamError("Could not reach OpenAI (network/timeouts).", details=str(e))
        except o.APIStatusError as e:
            raise UpstreamError(f"OpenAI API error {e.status_code}.", details=str(e))

    @staticmethod
    def _text(resp: Any) -> str:
        txt = getattr(resp, "output_text", None)
        if txt:
            return txt
        parts = []
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", "") == "message":
                for c in getattr(item, "content", None) or []:
                    if getattr(c, "type", "") == "output_text":
                        parts.append(getattr(c, "text", ""))
        return "".join(parts)

    # ------------------------------------------------------------------ LLM interface
    def vision_json(self, slices: list[Slice], system: str, prompt: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for s in slices:
            content.append({"type": "input_text", "text": f"Screenshot part {s.index} of {s.total} (top to bottom, parts overlap slightly):"})
            content.append({"type": "input_image", "detail": "high",
                            "image_url": f"data:{s.media_type};base64,{base64.b64encode(s.data).decode()}"})
        content.append({"type": "input_text", "text": prompt + "\nReturn the JSON object only."})
        resp = self._call(instructions=system, input=[{"role": "user", "content": content}],
                          text={"format": {"type": "json_object"}}, max_output_tokens=12000)
        return parse_json_object(self._text(resp))

    def text_json(self, system: str, prompt: str, max_tokens: int = 4000) -> dict[str, Any]:
        resp = self._call(instructions=system, input=prompt + "\nReturn the JSON object only.",
                          text={"format": {"type": "json_object"}}, max_output_tokens=max(max_tokens, 4000) * 2)
        return parse_json_object(self._text(resp))

    # ------------------------------------------------------------------ web search
    def web_search(self, query: str, country: Optional[str] = None) -> list[SearchHit]:
        prompt = (f"Use web search to find pages that sell or list this: {query}\n"
                  "Then reply with a plain list of up to 12 of the most relevant result page URLs, one per line, "
                  "each followed by its page title. Prefer product/package pages of sellers over blogs. No other commentary.")
        tool: dict[str, Any] = {"type": "web_search"}
        if self.settings.openai_search_context in ("low", "medium", "high"):
            tool["search_context_size"] = self.settings.openai_search_context
        if country:
            tool["user_location"] = {"type": "approximate", "country": country.upper()}
        try:
            resp = self._call(tools=[tool], input=prompt, max_output_tokens=3000)
        except UpstreamError as e:
            if len(tool) > 1 and "rejected the request" in e.message:
                log.warning("web_search optional params rejected; retrying plain tool (%s)", e.message[:120])
                resp = self._call(tools=[{"type": "web_search"}], input=prompt, max_output_tokens=3000)
            else:
                raise
        hits: dict[str, SearchHit] = {}
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", "") != "message":
                continue
            for c in getattr(item, "content", None) or []:
                for ann in getattr(c, "annotations", None) or []:
                    if getattr(ann, "type", "") == "url_citation" and getattr(ann, "url", None):
                        url = clean_openai_url(ann.url)
                        hits.setdefault(url, SearchHit(url=url, title=getattr(ann, "title", "") or "", query=query, engine="openai"))
        for url in URL_RE.findall(self._text(resp)):
            url = clean_openai_url(url)
            hits.setdefault(url, SearchHit(url=url, query=query, engine="openai"))
        return list(hits.values())


def build_llm(settings: Settings) -> OpenAILLM:
    problems = settings.validate_for_run()
    if problems:
        raise ConfigError(" ".join(problems))
    return OpenAILLM(settings)
