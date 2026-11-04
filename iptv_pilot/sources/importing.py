"""Import an M3U list as backup sources without touching primary sources."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from iptv_pilot.sources.m3u import M3UEntry, match_entries, parse_m3u_text, read_source
from iptv_pilot.sources.models import url_key
from iptv_pilot.sources.store import (
    create_source,
    duplicate_url,
    next_priority,
    sync_channel_mirror,
)


@dataclass
class ImportRow:
    channel_id: str | None
    channel_name: str | None
    matched_by: str | None
    url: str
    name: str
    action: str  # create | exists | unbound

    def as_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "matched_by": self.matched_by,
            "url": self.url,
            "name": self.name,
            "action": self.action,
        }


def source_origin_key(source_url: str) -> str:
    return f"m3u-import:{hashlib.sha256(source_url.encode()).hexdigest()[:16]}"


def preview_import(
    database,
    *,
    source: str,
    match: tuple[str, ...] = ("tvg_id", "name", "normalized_name"),
    limit: int = 1000,
) -> dict:
    text = read_source(source)
    entries = parse_m3u_text(text)
    channels = list(
        database.channels.find(
            {},
            {"channel_id": 1, "name": 1, "name_override": 1},
        )
    )
    channel_names = {row["channel_id"]: row.get("name_override") or row.get("name") for row in channels}
    results = match_entries(entries, channels, by=match)
    rows: list[ImportRow] = []
    seen: set[tuple[str | None, str]] = set()
    for result in results:
        entry: M3UEntry = result.entry
        channel_id = result.channel_id
        key = (channel_id, url_key(entry.url))
        if key in seen:
            continue
        seen.add(key)
        if channel_id is None:
            action = "unbound"
        elif duplicate_url(database, channel_id, entry.url):
            action = "exists"
        else:
            action = "create"
        rows.append(
            ImportRow(
                channel_id=channel_id,
                channel_name=channel_names.get(channel_id) if channel_id else None,
                matched_by=result.matched_by,
                url=entry.url,
                name=entry.name,
                action=action,
            )
        )
    counts = {
        "total": len(rows),
        "create": sum(1 for row in rows if row.action == "create"),
        "exists": sum(1 for row in rows if row.action == "exists"),
        "unbound": sum(1 for row in rows if row.action == "unbound"),
    }
    return {
        "rows": [row.as_dict() for row in rows[:limit]],
        "truncated": len(rows) > limit,
        "counts": counts,
        "origin_key": source_origin_key(source),
    }


def apply_import(
    database,
    *,
    source: str,
    match: tuple[str, ...] = ("tvg_id", "name", "normalized_name"),
) -> dict:
    preview = preview_import(database, source=source, match=match)
    origin_key = preview["origin_key"]
    created = 0
    for row in preview["rows"]:
        if row["action"] != "create":
            continue
        channel_id = row["channel_id"]
        create_source(
            database,
            channel_id=channel_id,
            url=row["url"],
            label=row["name"] or row["url"],
            kind="backup_m3u",
            provider="M3U 导入",
            origin="import",
            origin_key=f"{origin_key}:{row['name'][:80]}",
            priority=next_priority(database, channel_id),
        )
        created += 1
    # Sources from an earlier import of the same list that disappeared are
    # disabled, never deleted, so the user can still see what changed.
    imported = list(database.sources.find({"origin": "import"}))
    present = {url_key(row["url"]) for row in preview["rows"]}
    disabled = 0
    for source_row in imported:
        if source_row.get("url_key") in present or not source_row.get("enabled", True):
            continue
        database.sources.update_one(
            {"source_id": source_row["source_id"]},
            {"$set": {"enabled": False, "missing_from_seed": True}},
        )
        disabled += 1
    for channel_id in {row["channel_id"] for row in preview["rows"] if row["channel_id"]}:
        sync_channel_mirror(database, database.channels.find_one({"channel_id": channel_id}))
    return {
        "created": created,
        "disabled": disabled,
        "counts": preview["counts"],
        "origin_key": origin_key,
    }
