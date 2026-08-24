#!/usr/bin/env python3
"""Run a real short recording -> ASR -> LLM -> push-event smoke test."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def request(url: str, method: str = "GET", body: dict | None = None, token: str = ""):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5190")
    parser.add_argument("--token", default="")
    parser.add_argument("--channel-id")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--asr-model", default="base", choices=["tiny", "base", "small", "medium"])
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    channels = request(f"{base}/api/channels", token=args.token)
    if not channels:
        raise SystemExit("no enabled channels")
    channel_id = args.channel_id or channels[0]["channel_id"]
    keyword_rule = request(
        f"{base}/api/subscriptions",
        "POST",
        {
            "name": "end-to-end 关键词规则冒烟",
            "match_type": "keyword",
            "keyword": "新闻",
            "enabled": False,
        },
        args.token,
    )
    time_rule = request(
        f"{base}/api/subscriptions",
        "POST",
        {
            "name": "end-to-end 频道时间规则冒烟",
            "match_type": "channel_time",
            "channel_id": channel_id,
            "time_rule": {
                "weekdays": [0, 1, 2, 3, 4, 5, 6],
                "time": "19:00",
                "timezone": "Asia/Shanghai",
                "tolerance_min": 5,
            },
            "enabled": False,
        },
        args.token,
    )
    request(f"{base}/api/subscriptions/{keyword_rule['_id']}", "DELETE", token=args.token)
    request(f"{base}/api/subscriptions/{time_rule['_id']}", "DELETE", token=args.token)
    print("subscription_api=ok keyword=ok channel_time=ok", flush=True)
    created = request(
        f"{base}/api/recordings/manual",
        "POST",
        {
            "channel_id": channel_id,
            "duration_sec": args.duration,
            "program_title": "end-to-end 端到端冒烟测试",
            "asr_enabled": True,
            "asr_model": args.asr_model,
            "llm_enabled": True,
            "push_enabled": True,
        },
        args.token,
    )
    recording_id = created["_id"]
    deadline = time.monotonic() + args.timeout
    last_status = None
    while time.monotonic() < deadline:
        detail = request(f"{base}/api/recordings/{recording_id}", token=args.token)
        status = detail["recording"]["status"]
        if status != last_status:
            print(f"recording_id={recording_id} status={status}", flush=True)
            last_status = status
        if status in {"failed", "asr_failed", "summary_failed"}:
            raise SystemExit(f"pipeline failed: {detail['recording'].get('error')}")
        if detail.get("summary") and detail.get("push_events"):
            result = {
                "recording_id": recording_id,
                "channel_id": channel_id,
                "status": status,
                "size_bytes": detail["recording"].get("size_bytes"),
                "transcript_chars": len((detail.get("transcript") or {}).get("full_text", "")),
                "summary_chars": len((detail.get("summary") or {}).get("summary_text", "")),
                "push_events": [
                    {"provider": row["provider"], "status": row["status"]}
                    for row in detail["push_events"]
                ],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        time.sleep(5)
    raise SystemExit(f"timed out after {args.timeout}s; last_status={last_status}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
