"""The AI client: OpenRouter by default, key detection, streaming and error messages."""

import io
import json
import urllib.error

import pytest

from app.editor import llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in llm.KEY_NAMES + ("AI_PROVIDER", "AI_MODEL", "NEMOTRON_MODEL", "AI_BASE_URL", "NVIDIA_BASE_URL", "AI_REASONING",
                              "AI_EXTRA_BODY", "NVIDIA_EXTRA_BODY", "AI_MAX_TOKENS", "AI_VISION_MODEL", "AI_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


class Resp:
    def __init__(self, body=None, lines=None):
        self.body = json.dumps(body).encode() if body is not None else b""
        self.lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body

    def __iter__(self):
        return iter(self.lines)


def capture(monkeypatch, response):
    seen = {}

    def fake(req, timeout):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        return response(req) if callable(response) else response

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    return seen


def ok(text):
    return Resp({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


@pytest.mark.parametrize("name", ["OPENROUTER_API_KEY", "AI_API_KEY", "NVIDIA_API_KEY", "NEMOTRON_API_KEY"])
def test_an_openrouter_key_is_recognised_under_any_name(monkeypatch, name):
    monkeypatch.setenv(name, "sk-or-v1-abc")
    m = llm.from_env()
    assert m.provider == "openrouter"
    assert m.base_url == "https://openrouter.ai/api/v1"
    assert m.name == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert llm.describe(m) == {"enabled": True, "model": "nvidia/nemotron-3-ultra-550b-a55b:free", "provider": "OpenRouter"}


def test_openrouter_request_shape(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    seen = capture(monkeypatch, ok('{"reply":"hi"}'))
    assert llm.from_env().complete([{"role": "user", "content": "x"}]) == '{"reply":"hi"}'
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer sk-or-v1-abc"
    assert seen["headers"]["x-title"] and seen["headers"]["http-referer"]
    b = seen["body"]
    assert b["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free" and b["stream"] is False and b["max_tokens"] == 4096
    assert b["reasoning"] == {"effort": "low", "exclude": True}


def test_reasoning_off_and_overrides(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    monkeypatch.setenv("AI_REASONING", "off")
    monkeypatch.setenv("AI_MODEL", "some/other-model")
    monkeypatch.setenv("AI_MAX_TOKENS", "1000")
    monkeypatch.setenv("AI_EXTRA_BODY", '{"provider": {"sort": "throughput"}}')
    seen = capture(monkeypatch, ok("x"))
    llm.from_env().complete([])
    assert seen["body"]["reasoning"] == {"enabled": False}
    assert seen["body"]["model"] == "some/other-model" and seen["body"]["max_tokens"] == 1000
    assert seen["body"]["provider"] == {"sort": "throughput"}


def test_nvidia_and_custom_servers(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-abc")
    m = llm.from_env()
    assert m.provider == "nvidia" and "reasoning" not in m._body([], 10, False)
    monkeypatch.delenv("NVIDIA_API_KEY")
    monkeypatch.setenv("AI_API_KEY", "local")
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("AI_MODEL", "llama3")
    m = llm.from_env()
    assert m.provider == "custom" and m.base_url == "http://localhost:11434/v1" and m.name == "llama3"


def test_vision_model(monkeypatch):
    assert llm.vision_from_env() is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    assert llm.vision_from_env().name == "nvidia/nemotron-nano-12b-v2-vl:free"
    monkeypatch.setenv("AI_VISION_MODEL", "qwen/qwen2.5-vl-72b-instruct:free")
    assert llm.vision_from_env().name == "qwen/qwen2.5-vl-72b-instruct:free"


def test_streaming_reports_pieces(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    chunks = [
        b": OPENROUTER PROCESSING\n",
        b'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"{\\"reply\\":\\"He"}}]}\n',
        b"\n",
        b'data: {"choices":[{"delta":{"content":"llo\\"}"},"finish_reason":"stop"}]}\n',
        b"data: [DONE]\n",
    ]
    seen = capture(monkeypatch, Resp(lines=chunks))
    got = []
    out = llm.from_env().complete([], on_delta=lambda kind, text: got.append((kind, text)))
    assert out == '{"reply":"Hello"}'
    assert ("reasoning", "hmm") in got and [t for k, t in got if k == "content"] == ['{"reply":"He', 'llo"}']
    assert seen["body"]["stream"] is True and seen["headers"]["accept"] == "text/event-stream"


def http_error(status, message=""):
    def raiser(req):
        raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps({"error": {"message": message}}).encode()))
    return raiser


@pytest.mark.parametrize("status,message,needle", [
    (401, "", "rejected the API key"),
    (402, "", "out of credits"),
    (404, "", "doesn't offer the model"),
    (429, "Rate limit exceeded: free-models-per-day", "free AI allowance is used up"),
    (400, "bad", "HTTP 400"),
])
def test_error_messages(monkeypatch, status, message, needle):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secret")
    capture(monkeypatch, http_error(status, message))
    with pytest.raises(llm.LLMError, match=needle) as e:
        llm.from_env().complete([])
    assert "sk-or-v1-secret" not in str(e.value)


def test_busy_is_retried_then_reported(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    calls = []

    def busy(req):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 429, "x", {}, io.BytesIO(b"{}"))

    capture(monkeypatch, busy)
    with pytest.raises(llm.LLMError, match="busy"):
        llm.from_env().complete([])
    assert len(calls) == 3


def test_busy_then_success(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    state = {"n": 0}

    def flaky(req):
        state["n"] += 1
        if state["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b""))
        return ok("fine")

    capture(monkeypatch, flaky)
    assert llm.from_env().complete([]) == "fine"


def test_empty_answers(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    capture(monkeypatch, Resp({"choices": [{"message": {"content": None, "reasoning": "long thoughts"}, "finish_reason": "length"}]}))
    with pytest.raises(llm.LLMError, match="ran out of room"):
        llm.from_env().complete([])
    capture(monkeypatch, Resp({"error": {"message": "Provider returned error", "code": 502}}))
    with pytest.raises(llm.LLMError, match="reported a problem"):
        llm.from_env().complete([])


def test_think_tags_are_removed_even_when_unclosed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    capture(monkeypatch, ok("<think>a</think>answer"))
    assert llm.from_env().complete([]) == "answer"
    assert llm._clean("<think>unfinished") == ""
