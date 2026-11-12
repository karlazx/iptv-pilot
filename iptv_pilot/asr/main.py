from __future__ import annotations

import gc
import hashlib
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import ReturnDocument

from iptv_pilot.asr.audio import audio_windows
from iptv_pilot.asr.inference import configuration, get_session, runtime_info
from iptv_pilot.common.db import configure_logging, connect_database, run_forever
from iptv_pilot.common.embedding import embed_text
from iptv_pilot.common.runtime import is_draining

logger = logging.getLogger(__name__)


class ClaimLost(RuntimeError):
    pass


def stale_query(now: datetime) -> dict:
    cutoff = now - timedelta(seconds=int(os.getenv("ASR_STALE_SECONDS", "600")))
    return {"$or": [
        {"asr_heartbeat_at": {"$lte": cutoff}},
        {"asr_heartbeat_at": None, "asr_started_at": {"$lte": cutoff}},
    ]}


def claim_job(database):
    now = datetime.now(UTC)
    budget = max(1, int(os.getenv("ASR_MAX_RETRIES", "2")) + 1)
    # OOM/SIGKILL bypasses Python handlers: consume budget on claim.
    database.recordings.update_many(
        {"status": "asr_processing", "asr_attempt_count": {"$gte": budget}, **stale_query(now)},
        {"$set": {"status": "asr_failed", "error": "ASR worker exited; attempt budget exhausted"}},
    )
    if is_draining(database, "asr"):
        return None
    return database.recordings.find_one_and_update(
        {"asr_enabled": {"$ne": False}, "$and": [
            {"live_state": {"$nin": ["streaming", "sealed", "done"]}},
            {"$or": [
                {"asr_attempt_count": {"$lt": budget}}, {"asr_attempt_count": None},
            ]},
            {"$or": [
                {"status": "completed", "$or": [
                    {"next_asr_attempt_at": None}, {"next_asr_attempt_at": {"$lte": now}},
                ]},
                {"status": "asr_processing", **stale_query(now)},
            ]},
        ]},
        [{"$set": {"status": "asr_processing", "asr_started_at": now,
                   "asr_heartbeat_at": now, "asr_claim_id": uuid.uuid4().hex,
                   "asr_attempt_count": {"$add": [{"$ifNull": ["$asr_attempt_count", 0]}, 1]}}}],
        sort=[("completed_at", -1)], return_document=ReturnDocument.AFTER,
    )


def owner(recording: dict) -> dict:
    return {"_id": recording["_id"], "status": "asr_processing",
            "asr_claim_id": recording["asr_claim_id"]}


def owned_update(database, recording: dict, values: dict) -> None:
    result = database.recordings.update_one(owner(recording), {"$set": values})
    if not result.matched_count:
        raise ClaimLost("ASR claim was replaced")


def transcribe_window(audio, model_name: str) -> list[dict]:
    return get_session(model_name).transcribe(audio)


def transcribe_recording(database, recording: dict) -> tuple[list[dict], str]:
    backend = os.getenv("ASR_BACKEND", "mock")
    if backend == "mock":
        text = f"ASR mock transcript for {os.path.basename(recording['file_path'])}"
        return [{"start": 0.0, "end": 1.0, "text": text}], text
    if backend not in {"auto", "faster_whisper", "openvino"}:
        raise RuntimeError(f"unsupported ASR_BACKEND={backend}")
    path = Path(recording["file_path"])
    stat = path.stat()
    seconds = max(30, min(600, int(os.getenv("ASR_CHUNK_SECONDS", "300"))))
    model = recording.get("asr_model") or os.getenv("ASR_MODEL", "small")
    signature = hashlib.sha256(repr((str(path), stat.st_size, stat.st_mtime_ns, seconds,
                                    configuration(model))).encode()).hexdigest()
    deadline = time.monotonic() + max(60, int(os.getenv("ASR_JOB_TIMEOUT_SECONDS", "14400")))
    segments = []
    runtimes = []
    for index, (offset, audio) in enumerate(audio_windows(str(path), seconds)):
        if time.monotonic() > deadline:
            raise TimeoutError("ASR job exceeded its time budget")
        if not database.recordings.find_one(owner(recording), {"_id": 1}):
            raise ClaimLost("ASR claim was replaced")
        chunk_id = f"{recording['_id']}:{signature}:{index}"
        saved = database.asr_chunks.find_one({"_id": chunk_id})
        if saved is None:
            local = transcribe_window(audio, model)
            saved = {"_id": chunk_id, "recording_id": recording["_id"], "signature": signature,
                     "index": index, "runtime": runtime_info(), "segments": [
                         {**s, "start": s["start"] + offset, "end": s["end"] + offset}
                         for s in local], "created_at": datetime.now(UTC)}
            database.asr_chunks.update_one({"_id": chunk_id}, {"$setOnInsert": saved}, upsert=True)
        segments.extend(saved["segments"])
        actual = saved.get("runtime", {})
        if actual and actual not in runtimes:
            runtimes.append(actual)
        recording["asr_runtime"], recording["asr_runtimes"] = actual, runtimes
        owned_update(database, recording, {"asr_chunks_done": index + 1,
                     "asr_runtime": actual, "asr_runtimes": runtimes,
                     "asr_audio_seconds": offset + len(audio) / 16000,
                     "asr_progress_at": datetime.now(UTC)})
        del audio
        gc.collect()
        if os.getenv("LIVE_ASR_ENABLED", "0") == "1":
            from iptv_pilot.asr.live import run_once as live_once

            for _ in range(2):
                if not live_once(database):
                    break
    return segments, " ".join(s["text"] for s in segments).strip()


def process_recording(database, recording: dict) -> None:
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(20):
            try:
                owned_update(database, recording, {"asr_heartbeat_at": datetime.now(UTC)})
            except ClaimLost:
                return
            except Exception:
                logger.exception("ASR heartbeat failed")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        segments, full_text = transcribe_recording(database, recording)
        if not full_text:
            raise RuntimeError("ASR produced an empty transcript")
        document = {"recording_id": recording["_id"], "channel_id": recording["channel_id"],
                    "program_title": recording["program_title"], "start_time": recording["start_time"],
                    "segments": segments, "full_text": full_text,
                    "embedding_model": os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
                    "created_at": datetime.now(UTC), "asr_attempt_count": recording["asr_attempt_count"]}
        document["asr_runtime"] = recording.get("asr_runtime", {})
        document["asr_runtimes"] = recording.get("asr_runtimes", [])
        vector = embed_text(full_text)
        if vector:
            document["embedding"] = vector
        owned_update(database, recording, {"asr_progress_at": datetime.now(UTC)})
        database.transcripts.update_one(
            {"recording_id": recording["_id"], "$or": [
                {"asr_attempt_count": {"$lte": recording["asr_attempt_count"]}},
                {"asr_attempt_count": None}]}, {"$set": document}, upsert=True)
        transcript = database.transcripts.find_one({"recording_id": recording["_id"]})
        owned_update(database, recording, {"status": "asr_done", "asr_state": "done", "transcript_id": transcript["_id"],
                     "error": None, "next_asr_attempt_at": None, "asr_completed_at": datetime.now(UTC)})
        database.asr_chunks.delete_many({"recording_id": recording["_id"]})
        logger.info("ASR complete recording_id=%s", recording["_id"])
    except ClaimLost:
        logger.warning("ASR claim lost recording_id=%s", recording["_id"])
    except Exception as exc:
        budget = max(1, int(os.getenv("ASR_MAX_RETRIES", "2")) + 1)
        database.recordings.update_one(owner(recording), {"$set": {
            "error": str(exc)[:2000], "asr_retry_count": recording["asr_attempt_count"],
            "status": "completed" if recording["asr_attempt_count"] < budget else "asr_failed",
            "asr_state": "waiting" if recording["asr_attempt_count"] < budget else "failed",
            "next_asr_attempt_at": datetime.now(UTC) + timedelta(seconds=30),
        }})
        logger.exception("ASR failed recording_id=%s", recording["_id"])
    finally:
        stopped.set()
        thread.join(timeout=2)


def run_once(database) -> None:
    if os.getenv("LIVE_ASR_ENABLED", "0") == "1":
        from iptv_pilot.asr.live import run_once as live_once

        # A bounded live burst then a cooperative historical job avoids starvation.
        for _ in range(4):
            if not live_once(database):
                break
    else:
        database.recordings.update_many({"status": "completed", "live_state": {"$in": ["streaming", "sealed"]}},
                                       {"$set": {"live_state": "fallback", "asr_state": "waiting"}})
    recording = claim_job(database)
    if recording:
        process_recording(database, recording)


def main() -> None:
    configure_logging()
    database = connect_database()
    database.asr_chunks.create_index("recording_id")
    database.asr_chunks.create_index("created_at", expireAfterSeconds=604800)
    from iptv_pilot.asr.live import setup

    setup(database)
    interval = 1 if os.getenv("LIVE_ASR_ENABLED", "0") == "1" else float(os.getenv("ASR_POLL_SECONDS", "10"))
    run_forever(lambda: run_once(database), interval)


if __name__ == "__main__":
    main()
