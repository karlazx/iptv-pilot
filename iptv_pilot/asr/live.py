"""Durable closed-audio jobs, independent of capture's legacy status."""

import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import ReturnDocument

from iptv_pilot.asr.audio import audio_windows
from iptv_pilot.asr.inference import get_session
from iptv_pilot.common.embedding import embed_text
from iptv_pilot.common.live import (
    core_segments,
    merge_chunks,
    programme_bounds,
    programme_segments,
    segments_text,
)
from iptv_pilot.common.runtime import is_draining

LOG = logging.getLogger(__name__)
AUDIO_ROOT = Path("/data/live-audio")
_last_cleanup = 0.0


def remove_audio(path):
    target = Path(path).resolve()
    if target.is_relative_to(AUDIO_ROOT.resolve()) and target.suffix in {".wav", ".part"}:
        target.unlink(missing_ok=True)


def cleanup(database):
    global _last_cleanup
    if time.monotonic()-_last_cleanup < 3600:
        return
    _last_cleanup = time.monotonic()
    cutoff = datetime.now(UTC)-timedelta(days=1)
    for row in database.live_chunks.find({"created_at": {"$lt": cutoff},
                "state": {"$in": ["done", "cancelled", "failed"]}}).limit(1000):
        try:
            remove_audio(row["path"])
        except OSError:
            LOG.warning("audio cache cleanup deferred")
    if AUDIO_ROOT.exists():
        for path in AUDIO_ROOT.glob("*/*/*"):
            if path.suffix in {".part", ".wav"} and path.stat().st_mtime < cutoff.timestamp():
                # Active recordings retain their cache even after a day.
                if not database.recordings.find_one({"live_epoch": path.parent.name,
                                                       "status": {"$in": ["recording", "completed"]},
                                                       "live_state": {"$in": ["streaming", "sealed"]}}):
                    remove_audio(path)


def setup(database):
    database.live_chunks.create_index([("state", 1), ("created_at", 1)])
    database.live_chunks.create_index([("recording_id", 1), ("epoch", 1), ("lane", 1), ("seq", 1)], unique=True)
    database.live_notes.create_index([("recording_id", 1), ("epoch", 1), ("lane", 1), ("first", 1)], unique=True)
    database.live_chunks.create_index("created_at", expireAfterSeconds=7776000)
    database.live_notes.create_index("created_at", expireAfterSeconds=7776000)


def chunks(database, row):
    return list(database.live_chunks.find({"recording_id": row["_id"],
                "epoch": row["live_epoch"], "lane": "verified"}).sort("seq", 1))


def finalize(database):
    for row in database.recordings.find({"status": {"$in": ["recording", "completed"]},
                                         "live_state": {"$in": ["streaming", "sealed"]}}).limit(20):
        if row.get("claim_id", row["live_epoch"]) != row["live_epoch"]:
            continue
        rows = chunks(database, row)
        _, upper = programme_bounds(row)
        if row["status"] == "recording":
            if not row.get("end_time") or datetime.now(UTC) < row["end_time"]:
                continue
            prefix = []
            for item in rows:
                if item["seq"] != len(prefix) or item["state"] != "done":
                    break
                prefix.append(item)
                if item["offset"]+item["duration"] >= upper-.001:
                    break
            # The audio sample grid rounds datetimes by at most 1/16000 second.
            if not prefix or prefix[-1]["offset"]+prefix[-1]["duration"] < upper-.001:
                continue
            rows = prefix
        elif row["live_state"] != "sealed" or len(rows) != row.get("live_verified_count") or any(r["state"] != "done" for r in rows):
            continue
        segments = programme_segments(row, merge_chunks(rows))
        text = segments_text(segments)
        condition = {"_id": row["_id"], "status": row["status"], "live_epoch": row["live_epoch"],
                     "live_state": row["live_state"]}
        if not text:
            database.recordings.update_one(condition, {"$set": {
                "live_state": "fallback", "asr_state": "waiting", "live_error": "empty live transcript"}})
            continue
        document = {"recording_id": row["_id"], "channel_id": row["channel_id"],
                    "program_title": row["program_title"], "start_time": row["start_time"],
                    "segments": segments, "full_text": text, "live_epoch": row["live_epoch"], "verified": True,
                    "asr_runtime": rows[-1].get("runtime", {}), "created_at": datetime.now(UTC)}
        try:
            vector = embed_text(text)
            if vector:
                document.update(embedding=vector, embedding_model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"))
        except Exception:
            database.recordings.update_one(condition, {"$set": {"live_state": "fallback",
                                             "asr_state": "waiting", "live_error": "embedding unavailable"}})
            continue
        # Capture is terminal and the current epoch is sealed; no live writer can replace it.
        database.transcripts.update_one({"recording_id": row["_id"]}, {"$set": document}, upsert=True)
        transcript = database.transcripts.find_one({"recording_id": row["_id"]}, {"_id": 1})
        database.recordings.update_one(condition, {"$set": {
            "status": row["status"] if row["status"] == "recording" else "asr_done",
            "asr_state": "done", "live_state": "done",
            "transcript_id": transcript["_id"], "asr_completed_at": datetime.now(UTC),
            "asr_runtime": document["asr_runtime"], "error": None}})


def run_once(database):
    if is_draining(database, "asr"):
        return False
    cleanup(database)
    database.recordings.update_many({"status": "completed", "live_state": "streaming",
                                     "completed_at": {"$lt": datetime.now(UTC)-timedelta(minutes=2)}},
                                    {"$set": {"live_state": "fallback", "asr_state": "waiting"}})
    finalize(database)
    now = datetime.now(UTC)
    database.live_chunks.update_many({"state": "processing", "lease_until": {"$lt": now},
                                     "attempts": {"$gte": 3}}, {"$set": {"state": "failed"}})
    # Exhausted crashed jobs must let the existing offline path recover the recording.
    for failed in database.live_chunks.find({"state": "failed", "reconciled": {"$ne": True}}).limit(20):
        database.recordings.update_one({"_id": failed["recording_id"], "live_epoch": failed["epoch"],
                                        "live_state": {"$in": ["streaming", "sealed"]}},
                                       {"$set": {"live_state": "fallback", "asr_state": "waiting"}})
        database.live_chunks.update_one({"_id": failed["_id"]}, {"$set": {"reconciled": True}})
    job = database.live_chunks.find_one_and_update({"attempts": {"$lt": 3}, "$or": [
        {"state": "ready", "$or": [{"retry_at": None}, {"retry_at": {"$lte": now}}]},
        {"state": "processing", "lease_until": {"$lt": now}}]},
        {"$set": {"state": "processing", "claim": uuid.uuid4().hex,
                  "lease_until": now + timedelta(seconds=120)}, "$inc": {"attempts": 1}},
        sort=[("priority", 1), ("created_at", 1)], return_document=ReturnDocument.AFTER)
    if not job:
        return False
    owner = {"_id": job["_id"], "claim": job["claim"], "state": "processing"}
    row = database.recordings.find_one({"_id": job["recording_id"], "live_epoch": job["epoch"],
                                      "live_state": {"$in": ["streaming", "sealed"]},
                                      "status": {"$in": ["recording", "completed"]}})
    if not row or row.get("claim_id", job["epoch"]) != job["epoch"]:
        cancelled = database.live_chunks.update_one(owner, {"$set": {"state": "cancelled"}})
        if cancelled.matched_count:
            remove_audio(job["path"])
        return True
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(20):
            try:
                database.live_chunks.update_one(owner, {"$set": {
                    "lease_until": datetime.now(UTC) + timedelta(seconds=120)}})
            except Exception:
                LOG.warning("live ASR heartbeat deferred")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        windows = audio_windows(job["path"], max(60, int(job["duration"])+1))
        _, audio = next(windows)
        windows.close()
        if os.getenv("ASR_BACKEND") == "mock":
            left = job["offset"]-job.get("audio_offset", job["offset"])
            local = [{"start": left, "end": left+job["duration"], "text": "ASR mock live transcript"}]
            runtime = {"engine": "mock", "device": "cpu"}
        else:
            model = get_session(row.get("asr_model") or os.getenv("ASR_MODEL", "small"))
            local = model.transcribe(audio, words=job.get("lane") != "verified")
            runtime = model.metadata()
        segments = core_segments(job, local)
        result = database.live_chunks.update_one(owner, {"$set": {
            "state": "done", "segments": segments, "runtime": runtime,
            "inference_seconds": time.monotonic()-started,
            "done_at": datetime.now(UTC)}})
        if result.matched_count:
            database.recordings.update_one({"_id": row["_id"], "live_epoch": job["epoch"]}, {
                "$max": {"live_verified_seconds" if job.get("lane") == "verified" else "live_transcribed_seconds": job["offset"] + job["duration"]},
                "$set": {"asr_runtime": runtime, "asr_progress_at": datetime.now(UTC)}})
            try:
                remove_audio(job["path"])
            except OSError:
                LOG.warning("live audio cache removal failed")
    except Exception as exc:
        database.live_chunks.update_one(owner, {"$set": {
            "state": "failed" if job["attempts"] >= 3 else "ready",
            "retry_at": datetime.now(UTC) + timedelta(seconds=10), "error": type(exc).__name__}})
        LOG.warning("live chunk failed recording_id=%s attempt=%s", row["_id"], job["attempts"])
    finally:
        stopped.set()
        thread.join(timeout=2)
    finalize(database)
    return True
