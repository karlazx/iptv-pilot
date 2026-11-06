#!/usr/bin/env python3
"""Smoke checks for EPG substring search, live HLS proxy and range playback."""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request


def fetch(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read(), response.headers.get_content_type()


def first_hls_uri(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    attribute = re.search(r'URI="([^"]+)"', text)
    if attribute:
        return attribute.group(1)
    for line in text.splitlines():
        if line and not line.startswith("#"):
            return line
    raise AssertionError("HLS manifest contains no child URI")


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5190").rstrip("/")
    status, content, _ = fetch(f"{base}/api/epg/search?q={urllib.parse.quote('新闻')}&limit=100")
    rows = json.loads(content)
    assert status == 200 and rows, "Chinese EPG substring search returned no rows"
    assert any("新闻" in f"{row.get('title', '')}{row.get('description', '')}" for row in rows)
    status, scoped_content, _ = fetch(
        f"{base}/api/epg/search?q={urllib.parse.quote('新闻')}&channel={urllib.parse.quote('CCTV-1')}&limit=100"
    )
    scoped_rows = json.loads(scoped_content)
    assert status == 200 and scoped_rows
    assert all(row.get("channel_id") == "CCTV-1" for row in scoped_rows)

    status, playlist, media_type = fetch(
        f"{base}/api/channels/{urllib.parse.quote('Al Jazeera English')}/live"
    )
    assert status == 200 and playlist.lstrip().startswith(b"#EXTM3U")
    assert media_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl"}
    child = urllib.parse.urljoin(base, first_hls_uri(playlist))
    status, child_payload, _ = fetch(child)
    assert status == 200 and len(child_payload) > 1024
    if child_payload.lstrip().startswith(b"#EXTM3U"):
        segment = urllib.parse.urljoin(base, first_hls_uri(child_payload))
        status, segment_payload, _ = fetch(segment)
        assert status == 200 and len(segment_payload) > 1024

    recording_id = "6a7d44f940a3a92da9a8de17"
    status, ranged, _ = fetch(
        f"{base}/api/recordings/{recording_id}/play", {"Range": "bytes=0-1023"}
    )
    assert status == 206 and len(ranged) == 1024

    status, app_js, _ = fetch(f"{base}/static/app.js?v=11")
    assert status == 200
    for marker in ("openLivePlayer", "playback-rate", "searchEpg(loadProgramme)"):
        assert marker.encode() in app_js
    print(json.dumps({"epg_matches": len(rows), "scoped_matches": len(scoped_rows), "live_proxy": True, "range_playback": True}))


if __name__ == "__main__":
    main()
