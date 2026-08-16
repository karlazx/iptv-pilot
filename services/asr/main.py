from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument

from services.common.db import configure_logging, connect_database, run_forever
from services.common.embedding import embed_text

logger = logging.getLogger(__name__)
_whisper_models: dict[str, object] = {}


def transcribe(path: str, model_name: str) -> tuple[list[dict], str]:
    backend = os.getenv("ASR_BACKEND", "faster_whisper")
    if backend == "mock":
        text = f"ASR mock transcript for {os.path.basename(path)}"
        return [{"start": 0.0, "end": 1.0, "text": text}], text
    if backend != "faster_whisper":
        raise RuntimeError(f"unsupported ASR_BACKEND={backend}")
    from faster_whisper import WhisperModel

    model = _whisper_models.get(model_name)
    if model is None:
        model = WhisperModel(
            model_name,
            device=os.getenv("ASR_DEVICE", "cpu"),
            compute_type=os.getenv("ASR_COMPUTE_TYPE", "int8"),
        )
        _whisper_models[model_name] = model
    language = os.getenv("ASR_LANGUAGE", "").strip() or None
    raw_segments, _ = model.transcribe(path, vad_filter=True, language=language, beam_size=1)
    segments = [
        {"start": float(segment.start), "end": float(segment.end), "text": segment.text.strip()}
        for segment in raw_segments
    ]
    return segments, " ".join(segment["text"] for segment in segments).strip()


def run_once(database) -> None:
    now = datetime.now(UTC)
    retry_ready = {
        "$or": [
            {"next_asr_attempt_at": {"$exists": False}},
            {"next_asr_attempt_at": {"$lte": now}},
        ]
    }
    stale_before = now - timedelta(seconds=int(os.getenv("ASR_STALE_SECONDS", "600")))
    recording = database.recordings.find_one_and_update(
        {
            "asr_enabled": {"$ne": False},
            "$or": [
                {"status": "completed", **retry_ready},
                {"status": "asr_processing", "asr_started_at": {"$lte": stale_before}},
            ],
        },
        {"$set": {"status": "asr_processing", "asr_started_at": datetime.now(UTC)}},
        sort=[("completed_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not recording:
        return
    try:
        model_name = recording.get("asr_model") or os.getenv("ASR_MODEL", "small")
        segments, full_text = transcribe(recording["file_path"], model_name)
        if not full_text:
            raise RuntimeError("ASR produced an empty transcript")
        document = {
            "recording_id": recording["_id"],
            "channel_id": recording["channel_id"],
            "program_title": recording["program_title"],
            "start_time": recording["start_time"],
            "segments": segments,
            "full_text": full_text,
            "embedding_model": os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
            "created_at": datetime.now(UTC),
        }
        vector = embed_text(full_text)
        if vector:
            document["embedding"] = vector
        result = database.transcripts.update_one(
            {"recording_id": recording["_id"]}, {"$set": document}, upsert=True
        )
        transcript = database.transcripts.find_one({"recording_id": recording["_id"]})
        database.recordings.update_one(
            {"_id": recording["_id"]},
            {"$set": {"status": "asr_done", "transcript_id": transcript["_id"], "error": None, "next_asr_attempt_at": None}},
        )
        logger.info("ASR complete recording_id=%s upserted=%s", recording["_id"], bool(result.upserted_id))
    except Exception as exc:
        retry_count = int(recording.get("asr_retry_count", 0)) + 1
        maximum = int(os.getenv("ASR_MAX_RETRIES", "2"))
        update = {
            "asr_retry_count": retry_count,
            "error": str(exc)[:2000],
            "status": "completed" if retry_count <= maximum else "asr_failed",
        }
        if retry_count <= maximum:
            update["next_asr_attempt_at"] = datetime.now(UTC) + timedelta(seconds=30)
        database.recordings.update_one(
            {"_id": recording["_id"]},
            {"$set": update},
        )
        logger.exception("ASR failed recording_id=%s retry=%s/%s", recording["_id"], retry_count, maximum)


def main() -> None:
    configure_logging()
    database = connect_database()
    run_forever(lambda: run_once(database), float(os.getenv("ASR_POLL_SECONDS", "10")))


if __name__ == "__main__":
    main()
