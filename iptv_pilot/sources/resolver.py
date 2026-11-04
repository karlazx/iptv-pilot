"""Pure source-resolution helpers shared by web, recorder and scripts.

The module deliberately performs no I/O: callers decide what "usable" means and
persist the resulting active pointer themselves.  That keeps live playback
(which can try a source without an extra request) and the recorder (which just
needs the current source) on the same selection rules.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

LOG = logging.getLogger(__name__)


class SourceUnavailable(RuntimeError):
    """No enabled source could serve the request."""


def retry_after_seconds() -> float:
    return max(10.0, float(os.getenv("SOURCE_PRIMARY_RETRY_SECONDS", "60")))


def fail_threshold() -> int:
    return max(1, int(os.getenv("SOURCE_PRIMARY_FAIL_THRESHOLD", "3")))


def sources_for(database, channel: dict | None) -> list[dict]:
    if not channel or not channel.get("channel_id"):
        return []
    return list(database.sources.find({"channel_id": channel["channel_id"]}))


def _seconds_since(value: Any) -> float | None:
    if not value:
        return None
    try:
        return time.time() - value.timestamp()
    except AttributeError:
        return None


def retry_due(channel: dict) -> bool:
    """True when a channel in fallback state should probe its primary again."""
    age = _seconds_since(channel.get("primary_retry_at"))
    return age is None or age >= retry_after_seconds()


def candidates(database, channel: dict) -> list[dict]:
    """Ordered list of sources to try for playback.

    ``locked_primary`` (default) starts at the current active source.  When the
    active source is a fallback and the retry interval has elapsed, the locked
    primary is probed first so a recovered primary is restored immediately.
    """
    from iptv_pilot.sources.models import (
        ordered_sources,
        policy_for_channel,
        resolve_active,
        resolve_preferred,
    )

    rows = sources_for(database, channel)
    if not rows:
        return []
    policy = policy_for_channel(channel)
    preferred = resolve_preferred(rows, channel)
    active = resolve_active(rows, channel)
    enabled = ordered_sources(rows)

    if policy == "manual":
        return [preferred] if preferred else []

    head: list[dict] = []
    if active:
        head.append(active)
    if preferred and preferred.get("source_id") not in {row.get("source_id") for row in head}:
        if active and active.get("source_id") != preferred.get("source_id") and retry_due(channel):
            head.insert(0, preferred)
        else:
            head.append(preferred)
    for row in enabled:
        if row.get("source_id") not in {item.get("source_id") for item in head}:
            head.append(row)
    return head


def resolve_recording_source(database, channel: dict) -> dict:
    """Return the source a recording should pin at start time."""
    from iptv_pilot.sources.models import resolve_active

    rows = sources_for(database, channel)
    if not rows:
        raise SourceUnavailable("该频道没有可用的直播源")
    source = resolve_active(rows, channel)
    if not source:
        raise SourceUnavailable("该频道没有可用的直播源")
    return source


def mark_active(database, channel: dict, source: dict, state: str) -> None:
    """Persist the active pointer and mirror legacy channel fields."""
    from iptv_pilot.sources.models import channel_update_for_source

    database.channels.update_one(
        {"channel_id": channel["channel_id"]},
        channel_update_for_source(channel, source, state=state),
    )
    channel["active_source_id"] = source.get("source_id")
    channel["active_state"] = state
    channel["m3u_url"] = source.get("url")


def mark_retry_probe(database, channel: dict) -> None:
    from iptv_pilot.sources.models import now

    stamp = now()
    database.channels.update_one(
        {"channel_id": channel["channel_id"]},
        {"$set": {"primary_retry_at": stamp}},
    )
    channel["primary_retry_at"] = stamp


def record_success(database, source: dict, quality: dict | None = None) -> None:
    from iptv_pilot.sources.models import now

    update: dict[str, Any] = {"last_ok_at": now(), "last_error": None, "fail_count": 0}
    if quality is not None:
        update["quality"] = quality
    database.sources.update_one({"source_id": source["source_id"]}, {"$set": update})


def record_failure(database, source: dict, error: str, quality: dict | None = None) -> None:
    from iptv_pilot.sources.models import now

    update: dict[str, Any] = {"last_error": str(error)[:500], "last_fail_at": now()}
    if quality is not None:
        update["quality"] = quality
    database.sources.update_one(
        {"source_id": source["source_id"]},
        {"$set": update, "$inc": {"fail_count": 1}},
    )
