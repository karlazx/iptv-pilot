from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import ReturnDocument

from services.common.db import configure_logging, connect_database, run_forever

logger = logging.getLogger(__name__)
RECORDING_ROOT = Path("/data/recordings")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned[:100] or "untitled"


def claim_recording(database):
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=int(os.getenv("RECORDING_STALE_SECONDS", "600")))
    recording = database.recordings.find_one_and_update(
        {
            "scheduled_start": {"$lte": now},
            "$and": [
                {
                    "$or": [
                        {"status": "queued"},
                        {
                            "status": "recording",
                            "recording_started_at": {"$lte": stale_before},
                        },
                    ]
                },
                {
                    "$or": [
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": {"$lte": now}},
                    ]
                },
            ],
        },
        {"$set": {"status": "recording", "recording_started_at": now}},
        sort=[("scheduled_start", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if recording:
        _mark_partial_if_late(database, recording, now)
    return recording


def _mark_partial_if_late(database, recording: dict, now: datetime) -> None:
    scheduled_start = recording.get("scheduled_start")
    if not scheduled_start:
        return
    missed = int((now - scheduled_start).total_seconds())
    if missed <= 120:
        return
    database.recordings.update_one(
        {"_id": recording["_id"]},
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
        "-i",
        source_url,
        "-t",
        str(duration),
        "-c",
        "copy",
        str(output_path),
    ]
    subprocess.run(command, check=True, timeout=duration + 180)


def _capture_with_pyav(source_url: str, output_path: Path, duration: int) -> None:
    command = [
        sys.executable,
        "-m",
        "services.recorder.capture",
        source_url,
        str(output_path),
        str(duration),
    ]
    subprocess.run(command, check=True, timeout=duration + 180)


def capture_stream(source_url: str, output_path: Path, scheduled_end: datetime) -> str:
    """Capture a live stream, falling back to isolated PyAV when ffmpeg crashes."""
    initial_duration = max(1, int((scheduled_end - datetime.now(UTC)).total_seconds()))
    try:
        _capture_with_ffmpeg(source_url, output_path, initial_duration)
        return "ffmpeg"
    except Exception as ffmpeg_error:
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


def process_recording(database, recording: dict) -> None:
    channel = database.channels.find_one({"channel_id": recording["channel_id"], "enabled": True})
    if not channel or not channel.get("m3u_url"):
        raise RuntimeError(f"no enabled stream URL for channel {recording['channel_id']}")
    now = datetime.now(UTC)
    scheduled_end = recording.get("scheduled_end") or recording["end_time"]
    duration = max(1, int((scheduled_end - now).total_seconds()))
    channel_dir = RECORDING_ROOT / safe_name(recording["channel_id"])
    channel_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{safe_name(recording['program_title'])}_{recording['start_time']:%Y%m%d_%H%M}_"
        f"{str(recording['_id'])[-8:]}.ts"
    )
    output_path = channel_dir / filename
    temporary_path = output_path.with_suffix(".part.ts")
    logger.info("recording started id=%s output=%s duration=%s", recording["_id"], output_path, duration)
    try:
        temporary_path.unlink(missing_ok=True)
        capture_method = capture_stream(channel["m3u_url"], temporary_path, scheduled_end)
        size = temporary_path.stat().st_size
        minimum = int(os.getenv("RECORDING_MIN_BYTES", "1048576"))
        if size < minimum:
            raise RuntimeError(f"recording too small: {size} bytes")
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    database.recordings.update_one(
        {"_id": recording["_id"]},
        {
            "$set": {
                "status": "completed",
                "file_path": str(output_path),
                "duration_sec": duration,
                "size_bytes": size,
                "capture_method": capture_method,
                "completed_at": datetime.now(UTC),
                "next_attempt_at": None,
                "error": None,
                "epg_time_suspect": _epg_deviation_suspect(recording, output_path),
            }
        },
    )


def handle_failure(database, recording: dict, error: Exception) -> None:
    retry_count = int(recording.get("retry_count", 0)) + 1
    maximum = int(os.getenv("RECORDING_MAX_RETRIES", "2"))
    update = {
        "retry_count": retry_count,
        "error": str(error)[:2000],
        "updated_at": datetime.now(UTC),
    }
    scheduled_end = recording.get("scheduled_end") or datetime.now(UTC)
    if retry_count <= maximum and scheduled_end > datetime.now(UTC) + timedelta(seconds=10):
        update.update({"status": "queued", "next_attempt_at": datetime.now(UTC) + timedelta(seconds=30)})
    else:
        update["status"] = "failed"
    database.recordings.update_one({"_id": recording["_id"]}, {"$set": update})
    logger.exception("recording failed id=%s retry=%s/%s", recording["_id"], retry_count, maximum)


def run_once(database) -> None:
    recording = claim_recording(database)
    if not recording:
        return
    try:
        process_recording(database, recording)
    except Exception as exc:
        handle_failure(database, recording, exc)


def main() -> None:
    configure_logging()
    database = connect_database()
    run_forever(lambda: run_once(database), float(os.getenv("RECORDING_POLL_SECONDS", "5")))


if __name__ == "__main__":
    main()
