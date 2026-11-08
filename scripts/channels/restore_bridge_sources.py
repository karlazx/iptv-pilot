#!/usr/bin/env python3
"""Restore bridge HLS sources for channels that lost them.

A legacy ``channel-sync`` run overwrote ``m3u_url``/``stream_candidates`` with an
old ``result.m3u`` for channels whose sources had not yet been migrated into the
``sources`` collection.  This script rebuilds the canonical bridge URL from the
bridge playlist (``/rtp/<addr>`` -> ``/hls/<addr>/index.m3u8``), adds it back as
a locked primary source, and keeps the third-party URL as a backup.

The bridge address and playlist path are deployment-specific, so nothing is
hard-coded: pass them on the command line or through the environment.

Usage:
    export IPTV_BRIDGE_BASE=http://<bridge-host>:<port>/hls
    python -m scripts.channels.restore_bridge_sources --m3u /path/to/bridge.m3u --dry-run
    python -m scripts.channels.restore_bridge_sources --m3u /path/to/bridge.m3u --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from iptv_pilot.common.db import connect_database
from iptv_pilot.sources.models import make_source, now, url_key

ADDR_PATTERN = re.compile(r"/rtp/(\d{1,3}(?:\.\d{1,3}){3}:\d+)")
QUALITY_SUFFIX = re.compile(r"(超高清|高清|标清|HD|SD)$", re.IGNORECASE)


def bridge_base_from_env() -> str:
    value = os.getenv("IPTV_BRIDGE_BASE", "").strip()
    if not value:
        raise SystemExit(
            "IPTV_BRIDGE_BASE is required, for example "
            "http://<bridge-host>:<port>/hls (no trailing slash)"
        )
    return value.rstrip("/")


def normalize(value: str) -> str:
    cleaned = re.sub(r"[\s\-_·]+", "", (value or "").upper().strip())
    cleaned = re.sub(r"^\d+", "", cleaned)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = QUALITY_SUFFIX.sub("", cleaned)
    return cleaned


def load_bridge_map(path: str) -> dict[str, str]:
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    mapping: dict[str, str] = {}
    tvg = ""
    name = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            match = re.search(r'tvg-id="([^"]*)"', line)
            tvg = match.group(1) if match else ""
            name = line.rsplit(",", 1)[-1].strip()
            continue
        if not line or line.startswith("#"):
            continue
        match = ADDR_PATTERN.search(line)
        if not match:
            continue
        addr = match.group(1)
        for key in (tvg, name, re.sub(r"^\d+\s*", "", name)):
            if key:
                mapping.setdefault(key.upper(), addr)
                mapping.setdefault(normalize(key), addr)
    return mapping


def plan(database, bridge_map: dict[str, str], bridge_base: str) -> dict:
    damaged: list[dict] = []
    restored: list[dict] = []
    unresolved: list[str] = []

    channels = list(
        database.channels.find(
            {"enabled": True},
            {"channel_id": 1, "name": 1, "m3u_url": 1, "stream_candidates": 1, "preferred_source_id": 1},
        )
    )
    # Anything already served by this bridge host counts as "has a bridge source".
    bridge_host = urlparse(bridge_base).netloc

    for channel in channels:
        channel_id = channel.get("channel_id")
        urls = [channel.get("m3u_url") or ""] + list(channel.get("stream_candidates") or [])
        if any(bridge_host and bridge_host in url for url in urls if url):
            continue
        addr = (
            bridge_map.get(str(channel_id).upper())
            or bridge_map.get(normalize(channel_id or ""))
            or bridge_map.get(normalize(channel.get("name") or ""))
        )
        if not addr:
            unresolved.append(channel_id)
            continue
        bridge_url = f"{bridge_base}/{addr}/index.m3u8"
        damaged.append({"channel_id": channel_id, "current": channel.get("m3u_url"), "bridge": bridge_url})
        existing = list(database.sources.find({"channel_id": channel_id}))
        have_bridge = any(row.get("url") == bridge_url for row in existing)
        restored.append(
            {
                "channel_id": channel_id,
                "bridge": bridge_url,
                "backups": len(
                    [url for url in urls if url and (not bridge_host or bridge_host not in url)]
                ),
                "sources": len(existing),
                "has_bridge_source": have_bridge,
            }
        )
    return {"damaged": damaged, "restored": restored, "unresolved": unresolved}


def apply(database, bridge_map: dict[str, str], bridge_base: str) -> dict:
    result = plan(database, bridge_map)
    for row in result["damaged"]:
        channel_id = row["channel_id"]
        channel = database.channels.find_one({"channel_id": channel_id})
        if not channel:
            continue
        bridge_url = row["bridge"]
        existing = list(database.sources.find({"channel_id": channel_id}))
        bridge = next((item for item in existing if item.get("url") == bridge_url), None)
        if bridge is None:
            bridge = make_source(
                channel_id=channel_id,
                url=bridge_url,
                label=f"{channel.get('name') or channel_id} 官方组播",
                origin="migration",
                origin_key=f"bridge:{channel_id}",
                kind="udpxy",
                provider="官方组播",
                priority=0,
                locked=True,
            )
            database.sources.insert_one(bridge)
            database.sources.update_many(
                {"channel_id": channel_id, "source_id": {"$ne": bridge["source_id"]}},
                {"$inc": {"priority": 1}},
            )
        if row["current"] and row["current"] != bridge_url:
            if not database.sources.find_one({"channel_id": channel_id, "url_key": url_key(row["current"])}):
                backup = make_source(
                    channel_id=channel_id,
                    url=row["current"],
                    label=f"{channel.get('name') or channel_id} 第三方",
                    origin="migration",
                    origin_key=f"legacy:{channel_id}",
                    kind="m3u",
                    provider="第三方",
                    priority=1,
                )
                database.sources.insert_one(backup)
        rows = list(database.sources.find({"channel_id": channel_id}))
        database.sources.update_many({"channel_id": channel_id}, {"$set": {"locked": False}})
        database.sources.update_one({"source_id": bridge["source_id"]}, {"$set": {"locked": True, "enabled": True}})
        from iptv_pilot.sources.models import mirror_candidates

        database.channels.update_one(
            {"channel_id": channel_id},
            {
                "$set": {
                    "preferred_source_id": bridge["source_id"],
                    "active_source_id": bridge["source_id"],
                    "active_state": "primary",
                    "source_policy": "locked_primary",
                    "m3u_url": bridge_url,
                    "stream_candidates": mirror_candidates(rows),
                    "updated_at": now(),
                }
            },
        )
    return {
        "restored": len(result["restored"]),
        "unresolved": len(result["unresolved"]),
        "unresolved_ids": result["unresolved"][:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore bridge primary sources")
    parser.add_argument(
        "--m3u",
        default=os.getenv("IPTV_BRIDGE_M3U", ""),
        help="bridge playlist listing /rtp/<addr> URLs (default: IPTV_BRIDGE_M3U)",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the full plan as JSON")
    args = parser.parse_args()
    if not args.m3u:
        parser.error("--m3u or IPTV_BRIDGE_M3U is required")
    bridge_base = bridge_base_from_env()
    bridge_map = load_bridge_map(args.m3u)
    database = connect_database()
    if args.apply and not args.dry_run:
        print(json.dumps(apply(database, bridge_map, bridge_base), ensure_ascii=False))
        return
    result = plan(database, bridge_map, bridge_base)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"channels missing a bridge source: {len(result['damaged'])}")
    print(f"restorable: {len(result['restored'])}, unresolved: {len(result['unresolved'])}")
    for row in result["restored"][:15]:
        print(f"  {row['channel_id']}: bridge={row['bridge']} backups={row['backups']}")
    if result["unresolved"]:
        print("unresolved:", result["unresolved"][:20])


if __name__ == "__main__":
    main()
