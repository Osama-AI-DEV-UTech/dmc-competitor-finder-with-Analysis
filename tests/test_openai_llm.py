import json

import httpx
import openai
import pytest

from app.config import Settings
from app.errors import ConfigError, UpstreamError
from app.services.imageprep import make_slices
from app.services.llm import OpenAILLM
from tests.conftest import make_png

seen = []


def llm_with(handler):
    llm = OpenAILLM(Settings(openai_api_key="sk-test", openai_model="gpt-4.1"))
    llm.client = openai.OpenAI(api_key="sk-test", max_retries=0,
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return llm


def resp(text="", annotations=None):
    return {"id": "r", "object": "response", "created_at": 0, "status": "completed", "model": "gpt-4.1", "output": [
        {"type": "message", "id": "m", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": text, "annotations": annotations or []}]}],
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}


def test_text_and_vision_json():
    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json=resp('{"ok": true}'))

    llm = llm_with(handler)
    assert llm.text_json("sys", "hello") == {"ok": True}
    assert seen[-1]["text"]["format"]["type"] == "json_object" and seen[-1]["model"] == "gpt-4.1"
    slices = make_slices(make_png(1000, 4000))
    assert llm.vision_json(slices, "sys", "p") == {"ok": True}
    content = seen[-1]["input"][0]["content"]
    assert sum(1 for c in content if c["type"] == "input_image") == len(slices)
    assert content[1]["image_url"].startswith("data:image/") and content[1]["detail"] == "high"


def test_web_search_collects_citations_and_regional_tool():
    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        ann = [{"type": "url_citation", "url": "https://a.com/p?utm_source=openai", "title": "A", "start_index": 0, "end_index": 1}]
        return httpx.Response(200, json=resp("1. https://b.com/x - B page.", ann))

    hits = llm_with(handler).web_search("dubai package", "PK")
    assert {h.url for h in hits} == {"https://a.com/p", "https://b.com/x"}
    tool = seen[-1]["tools"][0]
    assert tool["type"] == "web_search" and tool["user_location"]["country"] == "PK" and tool["search_context_size"] == "high"


def test_error_mapping():
    def make(status, body):
        return llm_with(lambda r: httpx.Response(status, json={"error": body}))

    with pytest.raises(ConfigError, match="API key"):
        make(401, {"message": "bad key", "type": "invalid_request_error"}).text_json("s", "p")
    with pytest.raises(ConfigError, match="quota"):
        make(429, {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}).text_json("s", "p")
    with pytest.raises(UpstreamError):
        make(429, {"message": "slow down", "type": "rate_limit"}).text_json("s", "p")
    with pytest.raises(ConfigError, match="not found"):
        make(404, {"message": "model not found"}).text_json("s", "p")
    with pytest.raises(UpstreamError):
        make(500, {"message": "oops"}).text_json("s", "p")


def test_requires_key():
    with pytest.raises(ConfigError):
        OpenAILLM(Settings(openai_api_key=""))
