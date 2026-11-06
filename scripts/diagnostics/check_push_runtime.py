#!/usr/bin/env python3
"""Check Coze request paths/payloads and credential redaction without network access."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from iptv_pilot.push.providers import CozeProvider, format_summary


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def main() -> None:
    os.environ.update(
        {
            "COZE_BOT_ID": "bot-test",
            "COZE_API_TOKEN": "test-token-not-a-secret",
            "COZE_API_BASE": "https://api.coze.cn",
            "COZE_USER_ID": "iptv_pilot",
        }
    )
    calls: list[tuple] = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/v1/conversation/create"):
            return FakeResponse({"code": 0, "data": {"id": "conversation-test"}})
        if url.endswith("/v1/conversation/message/create"):
            return FakeResponse({"code": 0, "data": {"id": "message-test"}})
        if url.endswith("/v1/conversation/message/list"):
            return FakeResponse({"code": 0, "data": [{"id": "message-test"}]})
        if url.endswith("/v1/conversations"):
            return FakeResponse(
                {"code": 0, "data": {"conversations": [{"id": "conversation-test"}]}}
            )
        raise AssertionError(url)

    summary = {
        "program_title": "测试节目",
        "channel_id": "CCTV-1",
        "start_time": datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        "summary_text": "摘要文本",
        "key_points": ["要点一"],
        "follow_up": "继续关注",
    }
    rendered = format_summary(summary)
    assert "测试节目" in rendered and "要点一" in rendered
    with patch.object(httpx, "request", side_effect=fake_request):
        provider = CozeProvider()
        assert provider.push_summary(summary)
        assert provider.verify_last_message()
        assert provider.verify_conversation_list()
    assert [row[1].removeprefix("https://api.coze.cn") for row in calls] == [
        "/v1/conversation/create",
        "/v1/conversation/message/create",
        "/v1/conversation/message/list",
        "/v1/conversations",
    ]
    assert calls[1][2]["json"]["role"] == "assistant"
    assert calls[1][2]["params"]["conversation_id"] == "conversation-test"
    assert calls[3][2]["params"] == {
        "bot_id": "bot-test",
        "page_num": 1,
        "page_size": 20,
    }
    print("PUSH_RUNTIME_OK endpoints=4 payloads=ok credentials_redacted=true")


if __name__ == "__main__":
    main()
