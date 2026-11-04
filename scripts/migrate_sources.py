#!/usr/bin/env python3
"""Migrate legacy channel stream fields into the ``sources`` collection.

Idempotent: every legacy candidate becomes one source, and the channel's current
``m3u_url`` is locked as its primary so behaviour does not change on upgrade.

Usage:
    python -m scripts.migrate_sources --dry-run
    python -m scripts.migrate_sources --apply
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from iptv_pilot.common.db import connect_database
from iptv_pilot.sources.models import (
    make_source,
    mirror_candidates,
    now,
    ordered_sources,
    resolve_preferred,
    url_key,
)
from iptv_pilot.sources.store import sync_channel_mirror

LOG = logging.getLogger("migrate_sources")

PRIMARY_PATTERN_ENV = "SOURCE_PRIMARY_PATTERNS"


def primary_patterns(values: list[str] | None = None) -> list[str]:
    import os

    raw = values if values is not None else os.getenv(PRIMARY_PATTERN_ENV, "").split(",")
    return [value.strip() for value in raw if value and value.strip()]


def matches_primary(url: str, patterns: list[str]) -> bool:
    import re

    return any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)


def existing_by_channel(database) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in database.sources.find({}):
        grouped[row.get("channel_id")].append(row)
    return grouped


def plan(database, patterns: list[str] | None = None, *, force_primary: bool = False) -> dict:
    """Build the migration plan without writing anything."""
    patterns = patterns or primary_patterns()
    grouped = existing_by_channel(database)
    channels = list(
        database.channels.find(
            {},
            {
                "channel_id": 1,
                "name": 1,
                "m3u_url": 1,
                "stream_candidates": 1,
                "stream_manual": 1,
                "source": 1,
                "source_policy": 1,
                "preferred_source_id": 1,
            },
        )
    )
    custom_rows = list(database.custom_sources.find({}))

    plan_rows: list[dict] = []
    for channel in channels:
        channel_id = channel.get("channel_id")
        if not channel_id:
            continue
        existing = grouped.get(channel_id, [])
        existing_keys = {row.get("url_key") for row in existing}
        candidates = list(dict.fromkeys(channel.get("stream_candidates") or []))
        current = channel.get("m3u_url")
        if current and current not in candidates:
            candidates.insert(0, current)
        new_sources: list[dict] = []
        for index, url in enumerate(candidates):
            if not url or url_key(url) in existing_keys:
                continue
            new_sources.append(
                make_source(
                    channel_id=channel_id,
                    url=url,
                    label=f"{channel.get('name') or channel_id} 线路 {index + 1}",
                    origin="migration",
                    origin_key=f"legacy:{channel_id}",
                    kind=None,
                    priority=index,
                    locked=False,
                )
            )
            existing_keys.add(url_key(url))
        # Lock the pattern match when configured, otherwise keep the current
        # URL so upgrading never changes which line is in use.
        locked_source = None
        all_sources = existing + new_sources
        if patterns:
            locked_source = next(
                (row for row in all_sources if matches_primary(row.get("url", ""), patterns)),
                None,
            )
        if locked_source is None and current:
            locked_source = next((row for row in all_sources if row.get("url") == current), None)
        if locked_source is None and all_sources:
            locked_source = all_sources[0]
        plan_rows.append(
            {
                "channel_id": channel_id,
                "name": channel.get("name"),
                "existing": len(existing),
                "new": len(new_sources),
                "current": current,
                "locked_url": locked_source.get("url") if locked_source else None,
                "locked_by": "pattern" if locked_source and matches_primary(locked_source["url"], patterns) else "current",
                "policy": channel.get("source_policy") or ("manual" if channel.get("stream_manual") else "locked_primary"),
                "sources": [
                    {"url": row["url"], "new": row in new_sources, "locked": row is locked_source}
                    for row in existing + new_sources
                ],
            }
        )

    unbound_custom = [row for row in custom_rows if not row.get("channel_id")]
    bound_custom = [row for row in custom_rows if row.get("channel_id")]
    return {
        "channels": plan_rows,
        "custom_bound": len(bound_custom),
        "custom_unbound": len(unbound_custom),
        "patterns": patterns,
    }


def apply(database, patterns: list[str] | None = None, *, force_primary: bool = False) -> dict:
    result = plan(database, patterns, force_primary=force_primary)
    created = 0
    for row in result["channels"]:
        channel = database.channels.find_one({"channel_id": row["channel_id"]})
        if not channel:
            continue
        existing = list(database.sources.find({"channel_id": row["channel_id"]}))
        existing_keys = {item.get("url_key") for item in existing}
        candidates = list(dict.fromkeys(channel.get("stream_candidates") or []))
        current = channel.get("m3u_url")
        if current and current not in candidates:
            candidates.insert(0, current)
        added: list[dict] = []
        for index, url in enumerate(candidates):
            if not url or url_key(url) in existing_keys:
                continue
            document = make_source(
                channel_id=row["channel_id"],
                url=url,
                label=f"{channel.get('name') or row['channel_id']} 线路 {index + 1}",
                origin="migration",
                origin_key=f"legacy:{row['channel_id']}",
                priority=index,
            )
            database.sources.insert_one(document)
            existing_keys.add(document["url_key"])
            added.append(document)
            created += 1
        rows = list(database.sources.find({"channel_id": row["channel_id"]}))
        preferred = None
        if result["patterns"]:
            preferred = next(
                (item for item in rows if matches_primary(item.get("url", ""), result["patterns"])),
                None,
            )
        if preferred is None and current:
            preferred = next((item for item in rows if item.get("url") == current), None)
        if preferred is None and rows:
            preferred = ordered_sources(rows)[0]
        if preferred is not None:
            database.sources.update_many(
                {"channel_id": row["channel_id"]},
                {"$set": {"locked": False}},
            )
            database.sources.update_one(
                {"source_id": preferred["source_id"]},
                {"$set": {"locked": True, "enabled": True}},
            )
            database.channels.update_one(
                {"channel_id": row["channel_id"]},
                {
                    "$set": {
                        "preferred_source_id": preferred["source_id"],
                        "active_source_id": preferred["source_id"],
                        "active_state": "primary",
                        "source_policy": channel.get("source_policy")
                        or ("manual" if channel.get("stream_manual") else "locked_primary"),
                        "m3u_url": preferred.get("url"),
                        "stream_candidates": mirror_candidates(rows),
                        "updated_at": now(),
                    }
                },
            )
        sync_channel_mirror(database, database.channels.find_one({"channel_id": row["channel_id"]}))

    converted = 0
    for legacy in database.custom_sources.find({}):
        if database.sources.find_one({"url_key": url_key(legacy["url"]), "channel_id": legacy.get("channel_id")}):
            continue
        document = make_source(
            channel_id=legacy.get("channel_id"),
            url=legacy["url"],
            label=legacy.get("name") or legacy.get("url"),
            origin="custom",
            provider="",
            note=legacy.get("note", ""),
            enabled=legacy.get("enabled", True),
            priority=legacy.get("priority") or 100,
        )
        database.sources.insert_one(document)
        converted += 1
        if legacy.get("channel_id"):
            sync_channel_mirror(database, database.channels.find_one({"channel_id": legacy["channel_id"]}))
    return {
        "created": created,
        "custom_converted": converted,
        "channels": len(result["channels"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy streams into sources")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="only print the plan")
    parser.add_argument(
        "--primary-pattern",
        action="append",
        default=None,
        help="regex for URLs that should be locked as primary",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    database = connect_database()
    patterns = primary_patterns(args.primary_pattern)
    if args.apply and not args.dry_run:
        summary = apply(database, patterns)
        print(f"migration applied: {summary}")
    else:
        result = plan(database, patterns)
        print(f"channels: {len(result['channels'])}")
        print(f"bound custom sources: {result['custom_bound']}, unbound: {result['custom_unbound']}")
        missing = [row for row in result["channels"] if row["new"] == 0 and row["existing"] == 0]
        print(f"channels without any source: {len(missing)}")
        for row in result["channels"][:20]:
            print(
                f"  {row['channel_id']}: existing={row['existing']} new={row['new']} "
                f"locked={row['locked_url']} policy={row['policy']}"
            )
        print("dry run: no changes written")


if __name__ == "__main__":
    main()
