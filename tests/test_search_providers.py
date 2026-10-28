import pytest

from app.errors import ConfigError, UpstreamError
from app.services import search_providers as sp
from app.services.llm import SearchHit
from tests.conftest import FakeEngine


def test_multisearch_tolerates_failures():
    ms = sp.MultiSearch([FakeEngine("ok"), FakeEngine("bad", fail=True)])
    rep = ms.search_detailed("q", "IN")
    assert rep.hits and "bad" in rep.errors and rep.per_engine["ok"] > 0


def test_multisearch_all_fail_and_empty():
    with pytest.raises(UpstreamError):
        sp.MultiSearch([FakeEngine("a", fail=True), FakeEngine("b", fail=True)]).search_detailed("q")
    with pytest.raises(ConfigError):
        sp.MultiSearch([])


def test_build_search_engines_and_countries(monkeypatch):
    from app.config import Settings, _countries

    class L:  # stand-in for OpenAILLM
        def web_search(self, q, c=None):
            return [SearchHit("https://x.com")]

    s = Settings(openai_api_key="k", enable_ddg=True, enable_openai_search=True)
    assert [e.name for e in sp.build_search(s, L()).engines] == ["openai", "duckduckgo"]
    assert s.search_engines() == ["OpenAI web search", "DuckDuckGo"]
    monkeypatch.setenv("SEARCH_COUNTRIES", ",".join(f"{a}{b}" for a in "ABCDEFGHIJ" for b in "ABCDEFGHIJ"))
    assert _countries() == ["IN", "PK", "AE", "GB", "SG"]  # an "every country" list is ignored
    monkeypatch.setenv("SEARCH_COUNTRIES", "us, gb ,us,xx1")
    assert _countries() == ["US", "GB"]
