"""Persistent live-source records and channel-level source selection.

Each channel owns an ordered list of `sources` documents.  A channel keeps one
preferred ("primary") source and optionally several backups.  Automatic logic
may only change `active` state; it must never rewrite a locked source's URL,
priority or enabled flag.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

POLICIES = {"locked_primary", "auto", "manual"}
SOURCE_KINDS = {"udpxy", "m3u", "custom", "backup_m3u", "legacy"}
SOURCE_ORIGINS = {"seed", "custom", "import", "migration"}

DEFAULT_POLICY = "locked_primary"


def now() -> datetime:
    return datetime.now(UTC)


def url_key(url: str) -> str:
    """Normalize a source URL so duplicates are detected reliably."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return hashlib.sha256(
        urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, "")).encode()
    ).hexdigest()


def new_source_id() -> str:
    return uuid.uuid4().hex


def infer_kind(url: str, origin: str) -> str:
    """Classify a source for display purposes without changing behaviour."""
    lowered = url.lower()
    if "/rtp/" in lowered or ":4022" in lowered:
        return "udpxy"
    if lowered.endswith((".m3u", ".m3u8")) or "m3u8" in lowered:
        return "backup_m3u" if origin == "import" else "m3u"
    if origin == "import":
        return "backup_m3u"
    if origin in {"seed", "migration"}:
        return "m3u"
    return "custom"


def make_source(
    *,
    channel_id: str | None,
    url: str,
    label: str,
    origin: str,
    origin_key: str | None = None,
    kind: str | None = None,
    provider: str = "",
    priority: int = 0,
    locked: bool = False,
    enabled: bool = True,
    note: str = "",
) -> dict:
    stamp = now()
    return {
        "source_id": new_source_id(),
        "channel_id": channel_id,
        "url": url,
        "url_key": url_key(url),
        "kind": kind or infer_kind(url, origin),
        "provider": provider,
        "label": label,
        "note": note,
        "origin": origin,
        "origin_key": origin_key,
        "priority": priority,
        "locked": locked,
        "enabled": enabled,
        "missing_from_seed": False,
        "quality": None,
        "last_ok_at": None,
        "last_error": None,
        "last_fail_at": None,
        "fail_count": 0,
        "created_at": stamp,
        "updated_at": stamp,
    }


def policy_for_channel(channel: dict | None) -> str:
    if not channel:
        return DEFAULT_POLICY
    policy = channel.get("source_policy")
    if policy in POLICIES:
        return policy
    # Legacy documents only had a truthy stream_manual marker.
    if channel.get("stream_manual"):
        return "manual"
    return DEFAULT_POLICY


def ordered_sources(sources: Iterable[dict]) -> list[dict]:
    """Enabled sources first, ordered by priority then creation time."""
    rows = [row for row in sources if row.get("enabled", True)]
    rows.sort(
        key=lambda row: (
            int(row.get("priority") or 0),
            row.get("created_at") or datetime.min.replace(tzinfo=UTC),
            row.get("source_id") or "",
        )
    )
    return rows


def resolve_preferred(sources: list[dict], channel: dict) -> dict | None:
    """Return the channel's primary source, preferring the explicit pointer."""
    by_id = {row.get("source_id"): row for row in sources}
    preferred = by_id.get(channel.get("preferred_source_id"))
    if preferred and preferred.get("enabled", True):
        return preferred
    locked = [row for row in sources if row.get("locked") and row.get("enabled", True)]
    if locked:
        return ordered_sources(locked)[0]
    enabled = ordered_sources(sources)
    return enabled[0] if enabled else None


def resolve_active(sources: list[dict], channel: dict) -> dict | None:
    """Pick the source that should serve playback right now."""
    policy = policy_for_channel(channel)
    by_id = {row.get("source_id"): row for row in sources}
    preferred = resolve_preferred(sources, channel)

    if policy == "manual":
        return preferred

    active = by_id.get(channel.get("active_source_id"))
    if active and active.get("enabled", True):
        return active

    return preferred or (ordered_sources(sources)[0] if sources else None)


def fallback_sources(sources: list[dict], current: dict | None) -> list[dict]:
    """Enabled sources ordered after the current one, for automatic fallback."""
    rows = ordered_sources(sources)
    if current is None:
        return rows
    current_id = current.get("source_id")
    try:
        start = next(index for index, row in enumerate(rows) if row.get("source_id") == current_id)
    except StopIteration:
        return rows
    return rows[start + 1 :] + rows[:start]


def channel_update_for_source(channel: dict, source: dict, *, state: str) -> dict[str, Any]:
    """Mirror the chosen source onto the legacy channel fields."""
    stamp = now()
    return {
        "$set": {
            "active_source_id": source.get("source_id"),
            "active_state": state,
            "active_changed_at": stamp,
            "m3u_url": source.get("url"),
            "updated_at": stamp,
        }
    }


def mirror_candidates(sources: list[dict]) -> list[str]:
    return list(dict.fromkeys(row["url"] for row in ordered_sources(sources) if row.get("url")))


def source_public(row: dict) -> dict:
    """Shape a source document for the management UI."""
    return {
        "_id": row.get("_id"),
        "source_id": row.get("source_id"),
        "channel_id": row.get("channel_id"),
        "label": row.get("label"),
        "url": row.get("url"),
        "kind": row.get("kind"),
        "provider": row.get("provider"),
        "note": row.get("note"),
        "origin": row.get("origin"),
        "priority": row.get("priority", 0),
        "locked": bool(row.get("locked")),
        "enabled": bool(row.get("enabled", True)),
        "missing_from_seed": bool(row.get("missing_from_seed")),
        "quality": row.get("quality"),
        "last_ok_at": row.get("last_ok_at"),
        "last_error": row.get("last_error"),
        "last_fail_at": row.get("last_fail_at"),
        "fail_count": row.get("fail_count", 0),
        "created_at": row.get("created_at"),
    }
