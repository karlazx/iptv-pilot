"""CRUD helpers for the ``sources`` collection."""

from __future__ import annotations

import re
from typing import Any, Iterable

from iptv_pilot.sources.models import (
    infer_kind,
    make_source,
    mirror_candidates,
    new_source_id,
    now,
    ordered_sources,
    policy_for_channel,
    resolve_active,
    resolve_preferred,
    source_public,
    url_key,
)


def sources_for_channel(database, channel_id: str | None) -> list[dict]:
    if not channel_id:
        return []
    return list(database.sources.find({"channel_id": channel_id}))


def ordered_for_channel(database, channel_id: str | None) -> list[dict]:
    return ordered_sources(sources_for_channel(database, channel_id))


def unbound_sources(database) -> list[dict]:
    return list(database.sources.find({"channel_id": None}).sort("created_at", -1))


def next_priority(database, channel_id: str | None) -> int:
    rows = sources_for_channel(database, channel_id)
    return max((int(row.get("priority") or 0) for row in rows), default=-1) + 1


def update_quality(database, source: dict, quality: dict) -> None:
    """Store a benchmark result on a source without touching its selection state."""
    update: dict[str, Any] = {"quality": quality, "updated_at": now()}
    if quality.get("valid"):
        update["last_ok_at"] = now()
        update["last_error"] = None
    else:
        update["last_fail_at"] = now()
        update["last_error"] = str(quality.get("error") or "benchmark failed")[:500]
    database.sources.update_one({"source_id": source["source_id"]}, {"$set": update})


def sync_channel_mirror(database, channel: dict, rows: list[dict] | None = None) -> None:
    """Refresh ``m3u_url`` / ``stream_candidates`` from the source list."""
    if not channel or not channel.get("channel_id"):
        return
    rows = rows if rows is not None else sources_for_channel(database, channel["channel_id"])
    stored = {row.get("source_id"): row for row in rows}
    active = resolve_active(rows, channel)
    preferred = resolve_preferred(rows, channel)
    update: dict[str, Any] = {
        "stream_candidates": mirror_candidates(rows),
        "source_policy": policy_for_channel(channel),
        "updated_at": now(),
    }
    if active:
        update["m3u_url"] = active.get("url")
        update["active_source_id"] = active.get("source_id")
        update["active_state"] = (
            "primary"
            if preferred and active.get("source_id") == preferred.get("source_id")
            else "fallback"
        )
    if preferred:
        update["preferred_source_id"] = preferred.get("source_id")
    database.channels.update_one({"channel_id": channel["channel_id"]}, {"$set": update})
    channel.update(update)


def payload_for_channel(database, channel: dict) -> dict:
    rows = sources_for_channel(database, channel.get("channel_id"))
    preferred = resolve_preferred(rows, channel)
    active = resolve_active(rows, channel)
    ordered = ordered_sources(rows)
    return {
        "channel_id": channel.get("channel_id"),
        "policy": policy_for_channel(channel),
        "preferred_source_id": (preferred or {}).get("source_id"),
        "active_source_id": (active or {}).get("source_id"),
        "active_state": (
            "primary"
            if preferred and active and preferred.get("source_id") == active.get("source_id")
            else ("fallback" if active else "unavailable")
        ),
        "sources": [
            {
                **source_public(row),
                "current": bool(active and row.get("source_id") == active.get("source_id")),
                "preferred": bool(preferred and row.get("source_id") == preferred.get("source_id")),
            }
            for row in ordered
        ],
        "unbound_count": database.sources.count_documents({"channel_id": None}),
    }


def create_source(
    database,
    *,
    channel_id: str | None,
    url: str,
    label: str,
    kind: str | None = None,
    provider: str = "",
    note: str = "",
    enabled: bool = True,
    origin: str = "custom",
    origin_key: str | None = None,
    locked: bool = False,
    priority: int | None = None,
) -> dict:
    document = make_source(
        channel_id=channel_id,
        url=url,
        label=label,
        origin=origin,
        origin_key=origin_key,
        kind=kind or infer_kind(url, origin),
        provider=provider,
        note=note,
        enabled=enabled,
        locked=locked,
        priority=next_priority(database, channel_id) if priority is None else priority,
    )
    database.sources.insert_one(document)
    if channel_id:
        channel = database.channels.find_one({"channel_id": channel_id})
        sync_channel_mirror(database, channel)
        if not channel.get("preferred_source_id"):
            set_preferred(database, channel_id, document["source_id"])
    return document


def set_preferred(database, channel_id: str, source_id: str) -> dict:
    channel = database.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise KeyError("频道不存在")
    rows = sources_for_channel(database, channel_id)
    by_id = {row.get("source_id"): row for row in rows}
    source = by_id.get(source_id)
    if not source:
        raise KeyError("直播源不存在")
    database.sources.update_many({"channel_id": channel_id}, {"$set": {"locked": False}})
    database.sources.update_one({"source_id": source_id}, {"$set": {"locked": True, "enabled": True}})
    # Reorder: preferred first, the rest by previous priority.
    remaining = [row for row in ordered_sources(rows) if row.get("source_id") != source_id]
    database.sources.update_one({"source_id": source_id}, {"$set": {"priority": 0}})
    for index, row in enumerate(remaining, start=1):
        database.sources.update_one(
            {"source_id": row["source_id"]},
            {"$set": {"priority": index}},
        )
    database.channels.update_one(
        {"channel_id": channel_id},
        {
            "$set": {
                "preferred_source_id": source_id,
                "active_source_id": source_id,
                "active_state": "primary",
                "source_policy": "locked_primary",
                "m3u_url": source.get("url"),
                "updated_at": now(),
            }
        },
    )
    return database.channels.find_one({"channel_id": channel_id})


def activate(database, channel_id: str, source_id: str, *, temporary: bool) -> dict:
    channel = database.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise KeyError("频道不存在")
    source = database.sources.find_one({"channel_id": channel_id, "source_id": source_id})
    if not source:
        raise KeyError("直播源不存在")
    if temporary:
        update = {
            "active_source_id": source_id,
            "active_state": "fallback" if channel.get("preferred_source_id") != source_id else "primary",
            "m3u_url": source.get("url"),
            "updated_at": now(),
        }
        database.channels.update_one({"channel_id": channel_id}, {"$set": update})
        return database.channels.find_one({"channel_id": channel_id})
    return set_preferred(database, channel_id, source_id)


def reorder(database, channel_id: str, source_ids: Iterable[str]) -> None:
    rows = sources_for_channel(database, channel_id)
    known = {row.get("source_id") for row in rows}
    order = [value for value in source_ids if value in known]
    for index, source_id in enumerate(order):
        database.sources.update_one({"source_id": source_id}, {"$set": {"priority": index}})
    channel = database.channels.find_one({"channel_id": channel_id})
    sync_channel_mirror(database, channel)


def update_source(database, source_id: str, changes: dict[str, Any]) -> dict:
    source = database.sources.find_one({"source_id": source_id})
    if not source:
        raise KeyError("直播源不存在")
    update = {key: value for key, value in changes.items() if value is not None}
    if "url" in update:
        update["url_key"] = url_key(update["url"])
    update["updated_at"] = now()
    database.sources.update_one({"source_id": source_id}, {"$set": update})
    updated = database.sources.find_one({"source_id": source_id})
    if updated.get("channel_id"):
        channel = database.channels.find_one({"channel_id": updated["channel_id"]})
        if channel and channel.get("preferred_source_id") == source_id:
            database.channels.update_one(
                {"channel_id": channel["channel_id"]},
                {"$set": {"m3u_url": updated["url"], "updated_at": now()}},
            )
        sync_channel_mirror(database, channel)
    return updated


def delete_source(database, source_id: str) -> bool:
    source = database.sources.find_one({"source_id": source_id})
    if not source:
        return False
    if source.get("origin") == "seed":
        raise ValueError("内置源不能删除，只能禁用")
    channel_id = source.get("channel_id")
    database.sources.delete_one({"source_id": source_id})
    if channel_id:
        channel = database.channels.find_one({"channel_id": channel_id})
        if channel and channel.get("preferred_source_id") == source_id:
            replacement = ordered_for_channel(database, channel_id)
            database.channels.update_one(
                {"channel_id": channel_id},
                {
                    "$unset": {"preferred_source_id": ""},
                    **({"$set": {"active_source_id": replacement[0]["source_id"]}} if replacement else {}),
                },
            )
        sync_channel_mirror(database, database.channels.find_one({"channel_id": channel_id}))
    return True


def bind_source(database, source_id: str, channel_id: str | None) -> dict:
    source = database.sources.find_one({"source_id": source_id})
    if not source:
        raise KeyError("直播源不存在")
    previous = source.get("channel_id")
    priority = next_priority(database, channel_id) if channel_id else 0
    database.sources.update_one(
        {"source_id": source_id},
        {"$set": {"channel_id": channel_id, "priority": priority, "updated_at": now()}},
    )
    for target in {previous, channel_id}:
        if target:
            sync_channel_mirror(database, database.channels.find_one({"channel_id": target}))
    return database.sources.find_one({"source_id": source_id})


def duplicate_url(database, channel_id: str | None, url: str, *, exclude: str | None = None) -> dict | None:
    query: dict[str, Any] = {"channel_id": channel_id, "url_key": url_key(url)}
    if exclude:
        query["source_id"] = {"$ne": exclude}
    return database.sources.find_one(query)


def normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:100]


def new_id() -> str:
    return new_source_id()
