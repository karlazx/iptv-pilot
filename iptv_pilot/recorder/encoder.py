"""Transcode completed recordings to a smaller browser-playable MP4.

The recorder always captures an unmodified MPEG-TS so an unstable upstream can
be recorded safely.  This worker then optionally rewrites it to H.264/AAC MP4
at a capped resolution before ASR runs, so transcription reads a smaller file
and the original TS can be deleted straight away.

Claiming uses the same fencing pattern as the recorder: a ``claim_id`` guards
every terminal update, and a stale ``encode_heartbeat_at`` lets another worker
take over if this one dies mid-transcode.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import ReturnDocument

from iptv_pilot.common.db import configure_logging, connect_database
from iptv_pilot.recorder.encoding import load_settings

logger = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def _env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def encode_concurrency() -> int:
    return _env_int("RECORDING_ENCODE_CONCURRENCY", 1, 1, 4)


def lease_seconds() -> int:
    return _env_int("RECORDING_ENCODE_LEASE_SECONDS", 3600, 120)


def encode_poll_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("RECORDING_ENCODE_POLL_SECONDS", "15")))
    except ValueError:
        return 15.0


def _ffmpeg_executable() -> str:
    configured = os.getenv("FFMPEG_BIN", "auto").strip()
    if configured not in {"", "auto"}:
        return configured
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def encode_command(source: Path, output: Path, settings: dict) -> list[str]:
    """Build the ffmpeg command for a capped-resolution MP4 rewrite.

    Audio is always re-encoded to AAC: IPTV transport streams commonly carry
    MP2 audio, which cannot be muxed into MP4.
    """
    command = [
        _ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]
    height = int(settings.get("height") or 0)
    if height > 0:
        # -2 keeps the width even, as required by yuv420p.
        command += ["-vf", f"scale=-2:{height}"]
    command += [
        "-c:v",
        "libx264",
        "-preset",
        settings.get("preset") or "veryfast",
        "-crf",
        str(int(settings.get("crf") or 26)),
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]
    return command


def probe_duration(path: Path) -> float | None:
    """Read the container duration from ffmpeg's header probe.

    ``ffprobe`` is not shipped with imageio-ffmpeg, and the encoder image does
    not install PyAV, so parse the ``Duration:`` line ffmpeg prints while
    reading only the container header.
    """
    try:
        completed = subprocess.run(
            [_ffmpeg_executable(), "-hide_banner", "-nostdin", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        logger.warning("encode duration probe failed path=%s", path, exc_info=True)
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", completed.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def claim_job(database):
    """Claim a recording whose ASR/summary pipeline has finished.

    Encoding deliberately waits for the pipeline to reach a terminal state so
    it can never swap the file out from under the ASR worker, which reads
    ``file_path`` directly.  Recordings with ASR disabled are encoded as soon
    as the capture completes.
    """
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=lease_seconds())
    claim_id = uuid.uuid4().hex
    job = database.recordings.find_one_and_update(
        {
            "file_path": {"$exists": True},
            "$or": [
                {"status": {"$in": ["summarized", "asr_failed", "summary_failed"]}},
                {"status": "asr_done", "llm_enabled": False},
                {"status": "completed", "asr_enabled": False},
            ],
            "$and": [
                {
                    "$or": [
                        {"encode_status": "pending"},
                        {
                            "encode_status": "encoding",
                            "encode_heartbeat_at": {"$lte": stale_before},
                        },
                    ]
                },
                {
                    "$or": [
                        {"next_encode_attempt_at": {"$exists": False}},
                        {"next_encode_attempt_at": {"$lte": now}},
                    ]
                },
            ],
        },
        {
            "$set": {
                "encode_status": "encoding",
                "encode_claim_id": claim_id,
                "encode_worker": WORKER_ID,
                "encode_started_at": now,
                "encode_heartbeat_at": now,
            }
        },
        sort=[("completed_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return job


def output_path_for(source: Path) -> Path:
    return source.with_suffix(".mp4")


def _fail(database, recording: dict, claim_id: str | None, error: str) -> None:
    retry_count = int(recording.get("encode_retry_count", 0)) + 1
    maximum = _env_int("RECORDING_ENCODE_MAX_RETRIES", 2, 0)
    update = {
        "encode_retry_count": retry_count,
        "encode_error": error[:2000],
        "encode_heartbeat_at": datetime.now(UTC),
    }
    if retry_count <= maximum:
        update["encode_status"] = "pending"
        update["next_encode_attempt_at"] = datetime.now(UTC) + timedelta(minutes=10)
    else:
        # A failed encode must never block ASR or the summary pipeline.
        update["encode_status"] = "failed"
        update["next_encode_attempt_at"] = None
    database.recordings.update_one(
        {"_id": recording["_id"], "encode_claim_id": claim_id},
        {"$set": update},
    )
    logger.warning(
        "encoding failed id=%s retry=%s/%s error=%s",
        recording["_id"],
        retry_count,
        maximum,
        error,
    )


def encode_recording(database, recording: dict, settings: dict) -> None:
    claim_id = recording.get("encode_claim_id")
    source = Path(recording["file_path"])
    if not source.is_file():
        _fail(database, recording, claim_id, "recording file is missing")
        return
    if source.suffix.lower() in {".mp4", ".m4v"}:
        database.recordings.update_one(
            {"_id": recording["_id"], "encode_claim_id": claim_id},
            {"$set": {"encode_status": "skipped", "encode_reason": "already_mp4"}},
        )
        return
    output = output_path_for(source)
    temporary = output.with_suffix(".encoding.mp4")
    temporary.unlink(missing_ok=True)
    available = shutil.disk_usage(source.parent).free
    estimated = int(source.stat().st_size * 1.2)
    if available < estimated:
        _fail(
            database,
            recording,
            claim_id,
            f"not enough free space: need ~{estimated} bytes, free {available}",
        )
        return
    logger.info(
        "encoding started id=%s worker=%s source=%s height=%s crf=%s",
        recording["_id"],
        WORKER_ID,
        source,
        settings.get("height"),
        settings.get("crf"),
    )
    started = time.monotonic()
    try:
        subprocess.run(
            encode_command(source, temporary, settings),
            check=True,
            timeout=max(600, int(recording.get("duration_sec") or 0) * 30),
        )
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        _fail(database, recording, claim_id, f"encode failed: {exc}")
        return
    expected = float(recording.get("duration_sec") or 0)
    actual = probe_duration(temporary)
    if expected and actual and actual < expected * 0.8:
        temporary.unlink(missing_ok=True)
        _fail(
            database,
            recording,
            claim_id,
            f"encoded duration {actual:.0f}s is shorter than source {expected:.0f}s",
        )
        return
    encoded_size = temporary.stat().st_size
    original_size = source.stat().st_size
    try:
        temporary.replace(output)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        _fail(database, recording, claim_id, f"could not publish encoded file: {exc}")
        return
    result = database.recordings.update_one(
        {"_id": recording["_id"], "encode_claim_id": claim_id},
        {
            "$set": {
                "file_path": str(output),
                "encode_status": "done",
                "encoded_at": datetime.now(UTC),
                "encode_seconds": int(time.monotonic() - started),
                "encoded_size_bytes": encoded_size,
                "original_size_bytes": original_size,
                "size_bytes": encoded_size,
                "duration_sec": int(round(actual)) if actual else recording.get("duration_sec"),
                "encode_settings": {
                    "height": settings.get("height"),
                    "crf": settings.get("crf"),
                    "preset": settings.get("preset"),
                },
                "next_encode_attempt_at": None,
                "encode_error": None,
            }
        },
    )
    if result.matched_count == 0:
        output.unlink(missing_ok=True)
        logger.warning("encode claim lost id=%s claim=%s", recording["_id"], claim_id)
        return
    if not settings.get("keep_original"):
        source.unlink(missing_ok=True)
    logger.info(
        "encoding finished id=%s original_bytes=%s encoded_bytes=%s seconds=%s",
        recording["_id"],
        original_size,
        encoded_size,
        int(time.monotonic() - started),
    )


def requeue_pending(database) -> int:
    """Mark recordings the encoder has not seen yet.

    Covers rows created before rc.6 (no ``encode_status``) and rows recorded
    while compression was disabled.  Rows already marked ``done``/``skipped``
    are left alone so a restart never re-encodes the archive.
    """
    result = database.recordings.update_many(
        {
            "encode_status": {"$exists": False},
            "file_path": {"$exists": True},
            "$or": [
                {"status": {"$in": ["summarized", "asr_failed", "summary_failed"]}},
                {"status": "asr_done", "llm_enabled": False},
                {"status": "completed", "asr_enabled": False},
            ],
        },
        {"$set": {"encode_status": "pending"}},
    )
    return result.modified_count


def run_once(database) -> bool:
    settings = load_settings(database)
    if not settings.get("enabled"):
        return False
    job = claim_job(database)
    if not job:
        return False
    encode_recording(database, job, settings)
    return True


def main() -> None:
    configure_logging()
    database = connect_database()
    concurrency = encode_concurrency()
    logger.info("encoder starting worker=%s concurrency=%s", WORKER_ID, concurrency)
    if os.getenv("RECORDING_ENCODE_BACKLOG", "true").lower() in {"1", "true", "yes"}:
        queued = requeue_pending(database)
        if queued:
            logger.info("encoder queued recordings count=%s", queued)
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="encode") as pool:
        futures = set()
        while not stop.is_set():
            try:
                futures = {future for future in futures if not future.done()}
                settings = load_settings(database)
                if not settings.get("enabled"):
                    stop.wait(encode_poll_seconds())
                    continue
                while len(futures) < concurrency:
                    job = claim_job(database)
                    if not job:
                        break
                    futures.add(pool.submit(encode_recording, database, job, settings))
            except Exception:
                logger.exception("encoder iteration failed")
            stop.wait(encode_poll_seconds())


if __name__ == "__main__":
    main()
