from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from iptv_pilot.common.db import configure_logging, connect_database
from iptv_pilot.common.ffmpeg import ffmpeg_environment
from iptv_pilot.recorder.encoding import load_settings as load_encode_settings

logger = logging.getLogger(__name__)
RECORDING_ROOT = Path("/data/recordings")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\-.一-鿿]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned[:100] or "untitled"


def _env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def recording_concurrency() -> int:
    return _env_int("RECORDING_CONCURRENCY", 2, 1, 8)


def max_per_channel(concurrency: int | None = None) -> int:
    return _env_int("RECORDING_MAX_PER_CHANNEL", 2, 1, concurrency or recording_concurrency())


def late_grace_seconds() -> int:
    return _env_int("RECORDING_LATE_GRACE_SECONDS", 300, 0)


def lease_seconds() -> int:
    return _env_int("RECORDING_LEASE_SECONDS", 120, 30)


def heartbeat_seconds() -> int:
    return _env_int("RECORDING_HEARTBEAT_SECONDS", 30, 5, lease_seconds())


def lease_ttl_seconds() -> int:
    """How long a channel slot stays reserved without a heartbeat."""
    return max(120, heartbeat_seconds() * 4)


def min_window_seconds() -> int:
    return _env_int("RECORDING_MIN_WINDOW_SECONDS", 60, 10)


def retry_min_fraction() -> float:
    """Minimum share of the original window a retry must still cover.

    A retry that starts after most of the programme has aired records only the
    tail, fails the duration check, and burns another attempt. Require a
    meaningful share of the content to still be ahead of us.
    """
    try:
        value = float(os.getenv("RECORDING_RETRY_MIN_FRACTION", "0.2"))
    except ValueError:
        value = 0.2
    return min(1.0, max(0.0, value))


def retry_delay_seconds(error: str) -> int:
    """Back off longer when the upstream is rejecting us outright.

    A transport-level failure (5xx, connection refused, DNS) needs time to
    recover, while a short capture may simply be a single dropped segment.
    """
    normalized = (error or "").lower()
    if any(token in normalized for token in ("502", "503", "504", "bad gateway",
                                             "connection refused", "server error")):
        return _env_int("RECORDING_RETRY_UPSTREAM_DELAY_SECONDS", 120, 10)
    return _env_int("RECORDING_RETRY_DELAY_SECONDS", 30, 5)


def remaining_window(recording: dict, now: datetime | None = None) -> float:
    end = recording.get("scheduled_end") or recording.get("end_time")
    if not end:
        return 0.0
    return (end - (now or datetime.now(UTC))).total_seconds()


class RecordingHeartbeat:
    """Refresh ``heartbeat_at`` so long recordings are never reclaimed."""

    def __init__(self, database) -> None:
        self.database = database
        self._lock = threading.Lock()
        self._active: dict[str, tuple[object, str, datetime]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="recording-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def register(self, claim_id: str, recording_id, channel_id: str) -> None:
        with self._lock:
            self._active[claim_id] = (recording_id, channel_id, datetime.now(UTC))

    def unregister(self, claim_id: str) -> None:
        with self._lock:
            self._active.pop(claim_id, None)

    def _run(self) -> None:
        interval = heartbeat_seconds()
        ttl = lease_ttl_seconds()
        while not self._stop.wait(interval):
            with self._lock:
                active = list(self._active.items())
            if not active:
                continue
            now = datetime.now(UTC)
            for claim_id, (recording_id, channel_id, _) in active:
                try:
                    result = self.database.recordings.update_one(
                        {"_id": recording_id, "claim_id": claim_id},
                        {"$set": {"heartbeat_at": now}},
                    )
                    if not result.matched_count:
                        logger.warning("heartbeat lost claim id=%s claim=%s", recording_id, claim_id)
                        continue
                    self.database.recorder_leases.update_one(
                        {"_id": channel_id},
                        {"$set": {"claims.$[c].expires_at": now + timedelta(seconds=ttl)}},
                        array_filters=[{"c.claim_id": claim_id}],
                    )
                except Exception:
                    logger.warning("heartbeat update failed claim=%s", claim_id, exc_info=True)


def _claim_id() -> str:
    return uuid.uuid4().hex


def acquire_channel_slot(database, recording: dict, claim_id: str) -> bool:
    """Atomically reserve one of the per-channel capture slots."""
    channel_id = recording["channel_id"]
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=lease_ttl_seconds())
    database.recorder_leases.update_one(
        {"_id": channel_id},
        {"$pull": {"claims": {"expires_at": {"$lt": now}}}},
    )
    limit = max_per_channel()
    claim = {"claim_id": claim_id, "expires_at": expires}
    condition = {
        "_id": channel_id,
        "$expr": {
            "$lt": [
                {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$claims", []]},
                            "as": "slot",
                            "cond": {"$gt": ["$$slot.expires_at", now]},
                        }
                    }
                },
                limit,
            ]
        },
    }
    update = {
        "$push": {"claims": claim},
        "$set": {"updated_at": now},
    }
    if database.recorder_leases.find_one_and_update(
        condition, update, return_document=ReturnDocument.AFTER
    ) is not None:
        return True
    try:
        database.recorder_leases.insert_one(
            {"_id": channel_id, "claims": [claim], "updated_at": now}
        )
        return True
    except DuplicateKeyError:
        # Another worker created the lease first; retry the conditional update.
        return (
            database.recorder_leases.find_one_and_update(
                condition, update, return_document=ReturnDocument.AFTER
            )
            is not None
        )


def release_channel_slot(database, channel_id: str, claim_id: str) -> None:
    database.recorder_leases.update_one(
        {"_id": channel_id},
        {"$pull": {"claims": {"claim_id": claim_id}}},
    )


def requeue_claimed(database, recording: dict, claim_id: str, *, delay: int = 20) -> None:
    now = datetime.now(UTC)
    scheduled_end = recording.get("scheduled_end") or recording.get("end_time")
    if scheduled_end and scheduled_end <= now + timedelta(seconds=min_window_seconds()):
        database.recordings.update_one(
            {"_id": recording["_id"], "claim_id": claim_id},
            {
                "$set": {
                    "status": "failed",
                    "skip_reason": "overlap_wait_timeout",
                    "error": "no per-channel recording slot before the window closed",
                    "updated_at": now,
                }
            },
        )
        return
    database.recordings.update_one(
        {"_id": recording["_id"], "claim_id": claim_id},
        {
            "$set": {
                "status": "queued",
                "next_attempt_at": now + timedelta(seconds=delay),
            },
            "$unset": {"claim_id": "", "claimed_by": "", "heartbeat_at": ""},
        },
    )


def claim_recording(database, worker_id: str = WORKER_ID):
    from iptv_pilot.common.runtime import is_draining

    if is_draining(database, "recorder"):
        return None
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=lease_seconds() * 2)
    claim_id = _claim_id()
    recording = database.recordings.find_one_and_update(
        {
            "scheduled_start": {"$lte": now},
            "$expr": {
                "$gte": [
                    {"$ifNull": ["$scheduled_end", "$end_time"]},
                    now - timedelta(seconds=late_grace_seconds()),
                ]
            },
            "$and": [
                {
                    "$or": [
                        {"status": "queued"},
                        {
                            "$or": [
                                {"status": "recording", "heartbeat_at": {"$lte": stale_before}},
                                {
                                    "status": "recording",
                                    "heartbeat_at": {"$exists": False},
                                    "recording_started_at": {"$lte": stale_before},
                                },
                            ]
                        },
                    ]
                },
                {
                    "$or": [
                        {"next_attempt_at": None},
                        {"next_attempt_at": {"$lte": now}},
                    ]
                },
            ],
        },
        {
            "$set": {
                "status": "recording",
                "recording_started_at": now,
                "claim_id": claim_id,
                "claimed_by": worker_id,
                "heartbeat_at": now,
            },
            "$inc": {"attempt": 1},
        },
        sort=[("scheduled_start", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if recording:
        _mark_partial_if_late(database, recording, now, claim_id)
    return recording


def _mark_partial_if_late(database, recording: dict, now: datetime, claim_id: str) -> None:
    scheduled_start = recording.get("scheduled_start")
    if not scheduled_start:
        return
    missed = int((now - scheduled_start).total_seconds())
    if missed <= 120:
        return
    database.recordings.update_one(
        {"_id": recording["_id"], "claim_id": claim_id},
        {"$set": {"partial": True, "missed_seconds": max(0, missed)}},
    )
    recording["partial"] = True
    recording["missed_seconds"] = max(0, missed)
    logger.warning("recording claimed late id=%s missed_seconds=%s", recording["_id"], missed)


def _ffmpeg_executable() -> str:
    configured = os.getenv("FFMPEG_BIN", "auto").strip()
    if configured not in {"", "auto"}:
        return configured
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_media_duration(path: Path) -> float | None:
    """Return the real media duration in seconds, or None when it cannot be probed."""
    try:
        import av

        container = av.open(str(path))
        try:
            if container.duration:
                return container.duration / 1_000_000
        finally:
            container.close()
    except Exception:
        logger.warning("media duration probe failed path=%s", path, exc_info=True)
    return None


def _validate_captured_duration(path: Path, expected_seconds: int) -> float | None:
    """Reject captures that ended far too early, e.g. when the upstream stream hit EOF."""
    actual = probe_media_duration(path)
    if actual is None or expected_seconds < 60:
        return actual
    configured = float(os.getenv("RECORDING_MIN_DURATION_RATIO", "0.8"))
    minimum_ratio = min(1.0, max(0.1, configured))
    if actual < expected_seconds * minimum_ratio:
        raise RuntimeError(
            f"recording ended early: captured {actual:.1f}s of {expected_seconds}s"
        )
    return actual


def _epg_deviation_suspect(recording: dict, output_path: Path) -> bool:
    """Mark recordings whose real duration deviates >20% from the EPG-expected length."""
    end_time = recording.get("end_time")
    start_time = recording.get("start_time")
    if not end_time or not start_time:
        return False
    expected = (end_time - start_time).total_seconds()
    if expected <= 0:
        return False
    actual = probe_media_duration(output_path)
    if actual is None:
        return False
    deviation = abs(actual - expected) / expected
    if deviation > 0.2:
        logger.warning(
            "EPG time suspect id=%s expected=%.1fs actual=%.1fs deviation=%.1f%%",
            recording["_id"],
            expected,
            actual,
            deviation * 100,
        )
        return True
    return False


def _capture_with_ffmpeg(source_url: str, output_path: Path, duration: int) -> None:
    command = [
        _ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-y",
        "-rw_timeout",
        "15000000",
        "-i",
        source_url,
        "-t",
        str(duration),
        "-c",
        "copy",
        str(output_path),
    ]
    subprocess.run(command, check=True, timeout=duration + 180, env=ffmpeg_environment(command[0]))


def _capture_with_pyav(source_url: str, output_path: Path, duration: int) -> None:
    command = [
        sys.executable,
        "-m",
        "iptv_pilot.recorder.capture",
        source_url,
        str(output_path),
        str(duration),
    ]
    subprocess.run(command, check=True, timeout=duration + 180)


def capture_stream(source_url: str, output_path: Path, scheduled_end: datetime, on_partial=None) -> str:
    """Capture a live stream, falling back to isolated PyAV when ffmpeg crashes."""
    initial_duration = max(1, int((scheduled_end - datetime.now(UTC)).total_seconds()))
    try:
        _capture_with_ffmpeg(source_url, output_path, initial_duration)
        return "ffmpeg"
    except Exception as ffmpeg_error:
        if on_partial:
            on_partial(output_path, "ffmpeg")
        output_path.unlink(missing_ok=True)
        remaining = max(1, int((scheduled_end - datetime.now(UTC)).total_seconds()))
        if remaining <= 1:
            raise RuntimeError(f"ffmpeg capture failed after recording window: {ffmpeg_error}") from ffmpeg_error
        logger.warning(
            "ffmpeg capture failed; switching to isolated PyAV remaining=%ss error=%s",
            remaining,
            ffmpeg_error,
        )
        try:
            _capture_with_pyav(source_url, output_path, remaining)
            return "pyav"
        except Exception as pyav_error:
            raise RuntimeError(
                f"ffmpeg capture failed ({ffmpeg_error}); PyAV fallback failed ({pyav_error})"
            ) from pyav_error


def _pinned_source(recording: dict) -> dict | None:
    """Return the source snapshotted for this recording, if any."""
    if not recording.get("source_url"):
        return None
    return {
        "source_id": recording.get("source_id"),
        "url": recording["source_url"],
        "kind": recording.get("source_kind"),
    }


def _resolve_recording_source(database, channel: dict) -> dict:
    """Resolve once at claim time; the URL is pinned for the whole recording."""
    from iptv_pilot.sources import resolver

    try:
        return resolver.resolve_recording_source(database, channel)
    except resolver.SourceUnavailable:
        # Legacy channels may not have migrated source documents yet.
        if channel.get("m3u_url"):
            return {
                "source_id": None,
                "url": channel["m3u_url"],
                "kind": channel.get("source"),
            }
        raise


def same_source_retries() -> int:
    """How many times to retry the pinned source before trying another one."""
    return _env_int("RECORDING_SOURCE_RETRIES", 1, 0)


def max_source_switches() -> int:
    """Upper bound on how many different sources one task may try."""
    return _env_int("RECORDING_MAX_SOURCE_SWITCHES", 2, 0)


def _next_recording_source(database, recording: dict) -> dict | None:
    """Return an untried enabled source for the channel, or None.

    Sources are offered in priority order and each one is tried once; the
    recorder never falls back to a source it already failed on.
    """
    from iptv_pilot.sources import resolver
    from iptv_pilot.sources.models import ordered_sources

    channel = database.channels.find_one({"channel_id": recording["channel_id"]})
    if not channel:
        return None
    tried = {str(value) for value in (recording.get("tried_source_ids") or [])}
    if recording.get("source_id"):
        tried.add(str(recording["source_id"]))
    current_url = recording.get("source_url")
    for row in ordered_sources(resolver.sources_for(database, channel)):
        if str(row.get("source_id")) in tried:
            continue
        if row.get("url") and row.get("url") == current_url:
            continue
        return row
    return None


def process_recording(database, recording: dict) -> None:
    claim_id = recording.get("claim_id")
    encode_settings = load_encode_settings(database)
    channel = database.channels.find_one({"channel_id": recording["channel_id"], "enabled": True, "disabled": {"$ne": True}})
    if not channel:
        raise RuntimeError(f"no enabled stream URL for channel {recording['channel_id']}")
    source = _pinned_source(recording) or _resolve_recording_source(database, channel)
    if not source.get("url"):
        raise RuntimeError(f"no enabled stream URL for channel {recording['channel_id']}")
    if not recording.get("source_url"):
        database.recordings.update_one(
            {"_id": recording["_id"], "claim_id": claim_id},
            {
                "$set": {
                    "source_id": source.get("source_id"),
                    "source_url": source["url"],
                    "source_kind": source.get("kind"),
                }
            },
        )
        recording.update(
            {"source_id": source.get("source_id"), "source_url": source["url"], "source_kind": source.get("kind")}
        )
    now = datetime.now(UTC)
    recording["timeline_origin"] = recording.get("timeline_origin") or now
    scheduled_end = recording.get("scheduled_end") or recording["end_time"]
    duration = max(1, int((scheduled_end - now).total_seconds()))
    channel_dir = RECORDING_ROOT / safe_name(recording["channel_id"])
    channel_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{safe_name(recording['program_title'])}_{recording['start_time']:%Y%m%d_%H%M}_"
        f"{str(recording['_id'])[-8:]}_{claim_id[:8]}.ts"
    )
    output_path = channel_dir / filename
    temporary_path = output_path.with_suffix(".part.ts")
    logger.info(
        "recording started id=%s worker=%s claim=%s output=%s duration=%s source=%s",
        recording["_id"],
        recording.get("claimed_by") or "unknown",
        claim_id,
        output_path,
        duration,
        source.get("source_id") or "legacy",
    )
    database.recordings.update_one(
        {"_id": recording["_id"], "claim_id": claim_id},
        {"$set": {"capture_started_at": now, "timeline_origin": recording["timeline_origin"],
                   "capture_temporary_path": str(temporary_path), "capture_output_path": str(output_path)}},
    )
    temporary_path.unlink(missing_ok=True)
    recording["capture_started_at"] = now
    observer = None
    if os.getenv("LIVE_ASR_ENABLED", "0") == "1" and recording.get("asr_enabled", True) and recording.get("live_asr_enabled", True) and recording.get("asr_state") != "done" and not recording.get("recording_parts"):
        try:
            from iptv_pilot.recorder.live_audio import LiveAudio

            observer = LiveAudio(database, recording, temporary_path, _ffmpeg_executable())
            observer.start()
        except Exception:
            logger.warning("live ASR setup unavailable; recording continues", exc_info=True)
            observer = None
    try:
        from iptv_pilot.recorder.partials import part_metadata, report_source, save_partial

        attempt_start = [now]

        def preserve(path, method):
            if observer:
                observer.finish(False)
            save_partial(database, recording, path, attempt_start[0], method)
            try:
                report_source(database, recording, False)
            except Exception:
                logger.warning("source recording health update deferred")
            attempt_start[0] = datetime.now(UTC)

        capture_method = capture_stream(source["url"], temporary_path, scheduled_end, on_partial=preserve)
        if observer:
            try:
                observer.finish(True)
            except Exception:
                logger.warning("live ASR seal failed; recording continues", exc_info=True)
                database.recordings.update_one({"_id": recording["_id"], "claim_id": claim_id},
                                               {"$set": {"live_state": "fallback"}})
        size = temporary_path.stat().st_size
        minimum = int(os.getenv("RECORDING_MIN_BYTES", "1048576"))
        if size < minimum:
            raise RuntimeError(f"recording too small: {size} bytes")
        requested = max(1, int((scheduled_end-attempt_start[0]).total_seconds()))
        actual_duration = _validate_captured_duration(temporary_path, requested)
        epg_suspect = False if recording.get("recording_parts") else _epg_deviation_suspect(recording, temporary_path)
        temporary_path.replace(output_path)
        latest = database.recordings.find_one({"_id": recording["_id"], "claim_id": claim_id}, {"recording_parts": 1})
        if latest and latest.get("recording_parts"):
            recording["recording_parts"] = latest["recording_parts"]
        if recording.get("recording_parts"):
            part = part_metadata(recording, output_path, attempt_start[0], actual_duration or duration, capture_method)
            database.recordings.update_one({"_id": recording["_id"], "claim_id": claim_id},
                                          {"$push": {"recording_parts": part}})
        owned = database.recordings.update_one(
            {"_id": recording["_id"], "claim_id": claim_id},
            [{
                "$set": {
                    "status": {"$cond": [{"$eq": ["$asr_state", "done"]},
                        {"$switch": {"branches": [
                            {"case": {"$eq": ["$summary_state", "done"]}, "then": "summarized"},
                            {"case": {"$eq": ["$summary_state", "failed"]}, "then": "summary_failed"},
                            {"case": {"$eq": ["$summary_state", "finalizing"]}, "then": "summarizing"}],
                            "default": "asr_done"}}, "completed"]},
                    "capture_state": "partial" if recording.get("recording_parts") else "completed",
                    "file_path": str(output_path),
                    "asr_source_path": str(output_path),
                    "duration_sec": int(round(actual_duration)) if actual_duration else duration,
                    "size_bytes": size,
                    "capture_method": capture_method,
                    "capture_started_at": now,
                    "capture_ended_at": datetime.now(UTC),
                    "completed_at": datetime.now(UTC),
                    "next_attempt_at": None,
                    "error": None,
                    "skip_reason": None,
                    "epg_time_suspect": epg_suspect,
                    "encode_status": "pending" if encode_settings.get("enabled") else "disabled",
                    "encode_retry_count": 0,
                    "next_encode_attempt_at": None,
                }
            }],
        )
        if owned.matched_count == 0:
            logger.warning(
                "claim lost before publish id=%s claim=%s; leaving capture at %s",
                recording["_id"],
                claim_id,
                output_path,
            )
        else:
            try:
                report_source(database, recording, True, actual_duration or duration)
            except Exception:
                logger.warning("source recording health update deferred")
    except Exception:
        if observer:
            try:
                observer.finish(False)
            except Exception:
                logger.warning("live ASR abort failed", exc_info=True)
        try:
            from iptv_pilot.recorder.partials import save_partial

            save_partial(database, recording, temporary_path, attempt_start[0])
        except Exception:
            logger.warning("could not register partial recording; artifact retained", exc_info=True)
        raise


def handle_failure(database, recording: dict, error: Exception) -> None:
    """Retry the pinned source once, then rotate to an untried source.

    A short capture or a transient upstream error is often specific to one
    line, so the first retry keeps the same URL. Only after that does the
    recorder move to the next enabled source for the channel, which avoids
    stitching together feeds that differ in delay or content.
    """
    claim_id = recording.get("claim_id")
    try:
        from iptv_pilot.recorder.partials import report_source

        if not isinstance(error, OSError):
            report_source(database, recording, False)
    except Exception:
        logger.warning("source recording health update deferred")
    retry_count = int(recording.get("retry_count", 0)) + 1
    maximum = int(os.getenv("RECORDING_MAX_RETRIES", "2"))
    update = {
        "retry_count": retry_count,
        "error": str(error)[:2000],
        "updated_at": datetime.now(UTC),
        "capture_ended_at": datetime.now(UTC),
    }
    scheduled_end = recording.get("scheduled_end") or recording.get("end_time") or datetime.now(UTC)
    now = datetime.now(UTC)
    window_total = recording.get("window_seconds")
    if window_total is None:
        start = recording.get("scheduled_start")
        if start:
            window_total = (scheduled_end - start).total_seconds()
    remaining = (scheduled_end - now).total_seconds()
    needed = max(min_window_seconds(), (window_total or 0) * retry_min_fraction())
    window_ok = remaining > needed

    # ``source_attempts`` counts failures on the *current* source; it is what
    # decides when to rotate. ``retry_count`` is the lifetime counter and only
    # bounds total work, so it must not gate the rotation decision.
    attempts_on_source = int(recording.get("source_attempts", 0)) + 1
    switches = int(recording.get("source_switches", 0))
    tried = list(recording.get("tried_source_ids") or [])
    if recording.get("source_id") and str(recording["source_id"]) not in tried:
        tried.append(str(recording["source_id"]))

    switch_to = None
    exhausted_sources = False
    budget_left = attempts_on_source <= same_source_retries()
    if window_ok and not budget_left and switches < max_source_switches():
        switch_to = _next_recording_source(database, recording)
        if switch_to is None:
            exhausted_sources = True
        budget_left = switch_to is not None

    # A rotation resets the per-source counter, so cap the lifetime total too:
    # otherwise a channel with several backups could retry every source twice.
    total_attempts = retry_count + switches
    if window_ok and budget_left and total_attempts > maximum + max_source_switches():
        budget_left = False
        exhausted_sources = False

    if window_ok and budget_left:
        if switch_to is not None:
            update.update(
                {
                    "source_id": switch_to.get("source_id"),
                    "source_url": switch_to["url"],
                    "source_kind": switch_to.get("kind"),
                    "source_attempts": 0,
                    "source_switches": switches + 1,
                    "tried_source_ids": tried,
                    "source_switched_at": now,
                    "source_switched_reason": str(error)[:200],
                    "retry_count": 0,
                }
            )
            logger.warning(
                "recording source exhausted id=%s switch=%s/%s to source=%s",
                recording["_id"],
                switches + 1,
                max_source_switches(),
                switch_to.get("source_id") or switch_to["url"][:60],
            )
        else:
            update["source_attempts"] = attempts_on_source
            update["tried_source_ids"] = tried

    if window_ok and budget_left:
        delay = _env_int("RECORDING_RETRY_SWITCH_DELAY_SECONDS", 15, 1) if switch_to else retry_delay_seconds(str(error))
        update.update({"status": "queued", "next_attempt_at": now + timedelta(seconds=delay)})
        update["retry_window_remaining"] = int(remaining)
        result = database.recordings.update_one(
            {"_id": recording["_id"], "claim_id": claim_id},
            {
                "$set": update,
                "$unset": {"claim_id": "", "claimed_by": "", "heartbeat_at": ""},
            },
        )
    else:
        update["status"] = "failed"
        if exhausted_sources:
            # Every enabled source for the channel has now failed.
            update["skip_reason"] = "sources_exhausted"
        elif not window_ok:
            update["skip_reason"] = "window_expired" if remaining <= min_window_seconds() else "insufficient_window"
        else:
            update["skip_reason"] = "retry_exhausted"
        update["retry_window_remaining"] = int(max(0, remaining))
        result = database.recordings.update_one(
            {"_id": recording["_id"], "claim_id": claim_id},
            {"$set": update},
        )
    if result.matched_count == 0:
        logger.warning(
            "ignoring failure from stale claim id=%s claim=%s",
            recording["_id"],
            claim_id,
        )
        return
    if update.get("status") == "failed" and recording.get("recording_parts"):
        from iptv_pilot.recorder.partials import publish_partial

        publish_partial(database, recording, error)
    logger.exception("recording failed id=%s retry=%s/%s", recording["_id"], retry_count, maximum)


def run_recording(database, recording: dict, heartbeat: RecordingHeartbeat) -> None:
    claim_id = recording["claim_id"]
    channel_id = recording["channel_id"]
    if not acquire_channel_slot(database, recording, claim_id):
        requeue_claimed(database, recording, claim_id)
        logger.info("channel busy, requeued id=%s channel=%s", recording["_id"], channel_id)
        return
    heartbeat.register(claim_id, recording["_id"], channel_id)
    try:
        process_recording(database, recording)
    except Exception as exc:
        handle_failure(database, recording, exc)
    finally:
        heartbeat.unregister(claim_id)
        release_channel_slot(database, channel_id, claim_id)


def sweep_stale(database) -> None:
    """Recover abandoned claims and expire recordings whose window has closed."""
    now = datetime.now(UTC)
    grace = late_grace_seconds()
    stale_before = now - timedelta(seconds=lease_seconds() * 2)
    maximum = int(os.getenv("RECORDING_MAX_RETRIES", "2"))
    for recording in database.recordings.find(
        {
            "status": "recording",
            "$or": [
                {"heartbeat_at": {"$lte": stale_before}},
                {"heartbeat_at": {"$exists": False}, "recording_started_at": {"$lte": stale_before}},
            ],
        }
    ):
        claim_id = recording.get("claim_id")
        if recording.get("capture_temporary_path"):
            try:
                from iptv_pilot.recorder.partials import save_partial

                artifact = Path(recording["capture_temporary_path"])
                if not artifact.exists() and recording.get("capture_output_path"):
                    artifact = Path(recording["capture_output_path"])
                save_partial(database, recording, artifact,
                             recording.get("capture_started_at") or now, "worker_lost")
            except Exception:
                logger.warning("stale capture artifact recovery deferred")
        scheduled_end = recording.get("scheduled_end") or recording.get("end_time")
        if scheduled_end and scheduled_end < now - timedelta(seconds=grace):
            database.recordings.update_one(
                {"_id": recording["_id"], "claim_id": claim_id},
                {
                    "$set": {
                        "status": "failed",
                        "skip_reason": "window_expired",
                        "error": "recording window expired while worker was unresponsive",
                        "updated_at": now,
                    }
                },
            )
            continue
        retry_count = int(recording.get("retry_count", 0))
        if retry_count < maximum:
            database.recordings.update_one(
                {"_id": recording["_id"], "claim_id": claim_id},
                {
                    "$set": {
                        "status": "queued",
                        "next_attempt_at": now + timedelta(seconds=15),
                        "retry_count": retry_count + 1,
                        "updated_at": now,
                    },
                    "$unset": {"claim_id": "", "claimed_by": "", "heartbeat_at": ""},
                },
            )
            logger.warning("recovered stale claim id=%s previous=%s", recording["_id"], claim_id)
        else:
            database.recordings.update_one(
                {"_id": recording["_id"], "claim_id": claim_id},
                {
                    "$set": {
                        "status": "failed",
                        "error": "worker heartbeat lost",
                        "skip_reason": "claim_lost",
                        "updated_at": now,
                    }
                },
            )
    database.recordings.update_many(
        {
            "status": "queued",
            "$expr": {
                "$lt": [
                    {"$ifNull": ["$scheduled_end", "$end_time"]},
                    now - timedelta(seconds=grace),
                ]
            },
        },
        {
            "$set": {
                "status": "failed",
                "skip_reason": "window_expired",
                "error": "recording window expired before a worker was available",
                "updated_at": now,
            }
        },
    )
    database.recorder_leases.delete_many({"claims": {"$size": 0}})
    from iptv_pilot.recorder.partials import publish_partial

    for recording in database.recordings.find({"status": "failed", "has_saved_parts": True}):
        publish_partial(database, recording, recording.get("error", "capture interrupted"))


def run_once(database) -> None:
    """Claim and record one item synchronously; retained for tests and scripts."""
    recording = claim_recording(database)
    if not recording:
        return
    if not acquire_channel_slot(database, recording, recording["claim_id"]):
        requeue_claimed(database, recording, recording["claim_id"])
        return
    try:
        process_recording(database, recording)
    except Exception as exc:
        handle_failure(database, recording, exc)
    finally:
        release_channel_slot(database, recording["channel_id"], recording["claim_id"])


def main() -> None:
    configure_logging()
    database = connect_database()
    concurrency = recording_concurrency()
    poll = float(os.getenv("RECORDING_POLL_SECONDS", "5"))
    logger.info(
        "recorder starting worker=%s concurrency=%s max_per_channel=%s poll=%ss",
        WORKER_ID,
        concurrency,
        max_per_channel(concurrency),
        poll,
    )
    heartbeat = RecordingHeartbeat(database)
    heartbeat.start()
    sweep_stale(database)
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="recording") as pool:
        futures = set()
        last_sweep = time.monotonic()
        try:
            while True:
                finished = {future for future in futures if future.done()}
                futures -= finished
                for future in finished:
                    try:
                        future.result()
                    except Exception:
                        logger.exception("recording worker crashed")
                while len(futures) < concurrency:
                    recording = claim_recording(database, WORKER_ID)
                    if not recording:
                        break
                    futures.add(pool.submit(run_recording, database, recording, heartbeat))
                if time.monotonic() - last_sweep >= 30:
                    sweep_stale(database)
                    last_sweep = time.monotonic()
                time.sleep(poll)
        finally:
            heartbeat.stop()
            for future in futures:
                future.result()


if __name__ == "__main__":
    main()
