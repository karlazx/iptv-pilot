#!/usr/bin/env python3
"""Web HTTP regression test with exact cleanup of temporary records."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import httpx
from bson import ObjectId

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from iptv_pilot.common.db import connect_database  # noqa: E402


def require(response: httpx.Response, expected: int = 200) -> object:
    if response.status_code != expected:
        raise RuntimeError(f"{response.request.method} {response.request.url.path} -> {response.status_code}")
    if expected == 204:
        return None
    return response.json()


def main() -> None:
    base_url = os.getenv("WEB_BASE_URL", "http://localhost:8080")
    token = os.getenv("WEB_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    temporary_subscription_id: str | None = None
    database = connect_database()
    with httpx.Client(base_url=base_url, headers=headers, timeout=60) as client:
        try:
            require(client.get("/health"))
            root = client.get("/")
            if root.status_code != 200 or "IPTVPilot" not in root.text:
                raise RuntimeError("management console root is invalid")
            for asset in ("/static/app.css?v=7", "/static/app.js?v=7"):
                response = client.get(asset)
                if response.status_code != 200 or len(response.content) < 1000:
                    raise RuntimeError(f"static asset is invalid: {asset}")

            channels = require(client.get("/api/channels"))
            if not isinstance(channels, list) or not channels:
                raise RuntimeError("no playable channels")
            channel_id = channels[0]["channel_id"]
            require(client.get("/api/epg", params={"channel": channel_id, "date": date.today().isoformat()}))
            require(client.get("/api/epg/search", params={"q": "新闻", "limit": 5}))
            require(client.get("/api/recordings", params={"limit": 10}))
            summaries = require(client.get("/api/summaries/latest", params={"limit": 10}))
            require(client.get("/api/push/events", params={"limit": 10}))
            require(client.get("/api/system/status"))

            created = require(
                client.post(
                    "/api/subscriptions",
                    json={
                        "name": "Web 自动验收",
                        "match_type": "keyword",
                        "keyword": "__web_never_match__",
                        "channel_id": None,
                        "asr_enabled": True,
                        "asr_model": "tiny",
                        "llm_enabled": False,
                        "push_enabled": False,
                        "auto_bookmark": False,
                        "enabled": False,
                    },
                ),
                201,
            )
            temporary_subscription_id = created["_id"]
            changed = require(
                client.patch(
                    f"/api/subscriptions/{temporary_subscription_id}",
                    json={"name": "Web 自动验收（已编辑）"},
                )
            )
            if changed["name"] != "Web 自动验收（已编辑）":
                raise RuntimeError("subscription PATCH was not persisted")
            require(client.delete(f"/api/subscriptions/{temporary_subscription_id}"), 204)

            if summaries:
                bookmark = require(
                    client.post(
                        "/api/bookmarks",
                        json={
                            "user": "web_qa",
                            "item_type": "summary",
                            "item_id": summaries[0]["_id"],
                            "note": "temporary automated check",
                        },
                    ),
                    201,
                )
                require(client.delete(f"/api/bookmarks/{bookmark['_id']}"), 204)

            recordings = require(client.get("/api/recordings", params={"limit": 20}))
            playable = next((row for row in recordings if row.get("file_path")), None)
            if playable:
                recording_id = playable["_id"]
                require(client.get(f"/api/recordings/{recording_id}"))
                media = client.get(
                    f"/api/recordings/{recording_id}/play",
                    headers={**headers, "Range": "bytes=0-1023"},
                )
                if media.status_code != 206 or media.headers.get("content-type") != "video/mp4":
                    raise RuntimeError("recording Range playback failed")
        finally:
            if temporary_subscription_id:
                database.subscriptions.delete_one({"_id": ObjectId(temporary_subscription_id)})
            database.bookmarks.delete_many({"user": "web_qa"})

    print(
        "WEB_HTTP_OK "
        f"channels={len(channels)} summaries={len(summaries)} "
        f"playback={1 if playable else 0} temporary_records=0"
    )


if __name__ == "__main__":
    main()
