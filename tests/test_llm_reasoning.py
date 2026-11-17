import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from iptv_pilot.llm import providers, traceable


@pytest.fixture
def requests(monkeypatch):
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_MODEL", "synthetic-model")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    captured, clients = [], []

    def respond(request):
        captured.append(json.loads(request.content))
        content = {"summary_text": "Synthetic summary", "key_points": ["Synthetic fact"],
                   "entities": [], "follow_up": "", "evidence": []}
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion",
            "created": 0, "model": "synthetic-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": json.dumps(content)},
                "finish_reason": "stop"}]})

    def client(**kwargs):
        instance = OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        clients.append(instance)
        return instance

    monkeypatch.setattr(providers, "OpenAI", client)
    yield captured
    for instance in clients:
        instance.close()


@pytest.mark.parametrize("effort", [None, "", "  ", "none", "minimal", "low", "medium", "high", "xhigh", "max", " LOW "])
@pytest.mark.parametrize("grounded", [False, True])
def test_reasoning_strength_is_optional_and_reaches_summary_request(requests, monkeypatch, effort, grounded):
    if effort is not None:
        monkeypatch.setenv("LLM_REASONING_EFFORT", effort)
    provider = providers.create_provider()
    if grounded:
        result = provider.summarize_grounded({"segments": [{"id": "s1", "text": "Synthetic fact"}]})
    else:
        result = provider.summarize("Synthetic fact")
    assert result["summary_text"] == "Synthetic summary"
    assert len(requests) == 1
    expected = (effort or "").strip().lower()
    if expected:
        assert requests[0]["reasoning_effort"] == expected
    else:
        assert "reasoning_effort" not in requests[0]
    assert requests[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("provider_name", ["ark", "openai", "custom"])
def test_reasoning_setting_applies_to_compatible_providers(requests, monkeypatch, provider_name):
    monkeypatch.setenv("LLM_PROVIDER", provider_name)
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    monkeypatch.setenv("ARK_API_KEY", "synthetic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    providers.create_provider().summarize("Synthetic fact")
    assert requests[0]["reasoning_effort"] == "low"


def test_invalid_reasoning_setting_fails_before_request(requests, monkeypatch):
    monkeypatch.setenv("LLM_REASONING_EFFORT", "unsupported")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        providers.create_provider()
    assert requests == []


def test_changed_reasoning_setting_recomputes_notes_and_default_reuses_legacy_cache(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_MODEL", "synthetic-model")
    notes, calls = {}, []

    def upsert(query, update, **kwargs):
        notes.setdefault(query["_id"], update["$setOnInsert"])

    database = SimpleNamespace(analysis_notes=SimpleNamespace(
        find_one=lambda query: notes.get(query["_id"]), update_one=upsert))
    recording = {"_id": "synthetic-recording"}
    segments = traceable.identify([{"start": 0, "end": 1, "text": "Synthetic fact"}])
    legacy_digest = hashlib.sha256(json.dumps([[(s["id"], s["text"]) for s in segments],
        "custom", "synthetic-model", "evidence-v1"], ensure_ascii=False).encode()).hexdigest()
    legacy_id = recording["_id"] + ":" + legacy_digest
    notes[legacy_id] = {"_id": legacy_id, "summary_text": "Previously cached"}

    class Provider(providers.MockProvider):
        reasoning_effort = None

        def summarize_grounded(self, payload):
            calls.append(self.reasoning_effort)
            return super().summarize_grounded(payload)

    provider = Provider()
    assert traceable.cached_note(database, recording, segments, provider)["_id"] == legacy_id
    assert calls == []
    provider.reasoning_effort = "low"
    low = traceable.cached_note(database, recording, segments, provider)
    assert traceable.cached_note(database, recording, segments, provider)["_id"] == low["_id"]
    assert calls == ["low"]
    provider.reasoning_effort = "high"
    high = traceable.cached_note(database, recording, segments, provider)
    assert high["_id"] != low["_id"]
    assert calls == ["low", "high"]
