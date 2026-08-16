from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument

from services.common.db import configure_logging, connect_database, run_forever
from services.llm.providers import create_provider

logger = logging.getLogger(__name__)


def run_once(database) -> None:
    now = datetime.now(UTC)
    retry_ready = {
        "$or": [
            {"next_summary_attempt_at": {"$exists": False}},
            {"next_summary_attempt_at": {"$lte": now}},
        ]
    }
    stale_before = now - timedelta(seconds=int(os.getenv("LLM_STALE_SECONDS", "300")))
    recording = database.recordings.find_one_and_update(
        {
            "llm_enabled": {"$ne": False},
            "$or": [
                {"status": "asr_done", **retry_ready},
                {"status": "summarizing", "summary_started_at": {"$lte": stale_before}},
            ],
        },
        {"$set": {"status": "summarizing", "summary_started_at": datetime.now(UTC)}},
        sort=[("start_time", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not recording:
        return
    try:
        transcript = database.transcripts.find_one({"recording_id": recording["_id"]})
        if not transcript:
            raise RuntimeError("transcript not found")
        if not transcript.get("full_text", "").strip():
            raise RuntimeError("transcript is empty")
        maximum = int(os.getenv("LLM_MAX_INPUT_CHARS", "60000"))
        summary = create_provider().summarize(transcript.get("full_text", "")[:maximum])
        document = {
            "recording_id": recording["_id"],
            "transcript_id": transcript["_id"],
            "program_title": recording["program_title"],
            "channel_id": recording["channel_id"],
            "start_time": recording.get("start_time"),
            **summary,
            "pushed": False,
            "push_enabled": recording.get("push_enabled", True),
            "push_processed": False,
            "llm_provider": os.getenv("LLM_PROVIDER", "ark"),
            "llm_model": os.getenv("LLM_MODEL", ""),
            "created_at": datetime.now(UTC),
        }
        database.summaries.update_one(
            {"recording_id": recording["_id"]}, {"$set": document}, upsert=True
        )
        database.recordings.update_one(
            {"_id": recording["_id"]},
            {"$set": {"status": "summarized", "error": None, "next_summary_attempt_at": None}},
        )
        if recording.get("auto_bookmark"):
            saved_summary = database.summaries.find_one({"recording_id": recording["_id"]})
            database.bookmarks.update_one(
                {"user": "default", "item_type": "summary", "item_id": saved_summary["_id"]},
                {
                    "$setOnInsert": {
                        "user": "default",
                        "item_type": "summary",
                        "item_id": saved_summary["_id"],
                        "note": "订阅规则自动收藏",
                        "created_at": datetime.now(UTC),
                    }
                },
                upsert=True,
            )
        logger.info("summary complete recording_id=%s", recording["_id"])
    except Exception as exc:
        retry_count = int(recording.get("summary_retry_count", 0)) + 1
        maximum = int(os.getenv("LLM_MAX_RETRIES", "2"))
        update = {
            "summary_retry_count": retry_count,
            "error": str(exc)[:2000],
            "status": "asr_done" if retry_count <= maximum else "summary_failed",
        }
        if retry_count <= maximum:
            update["next_summary_attempt_at"] = datetime.now(UTC) + timedelta(seconds=30)
        database.recordings.update_one(
            {"_id": recording["_id"]},
            {"$set": update},
        )
        logger.exception("summary failed recording_id=%s retry=%s/%s", recording["_id"], retry_count, maximum)


def main() -> None:
    configure_logging()
    database = connect_database()
    run_forever(lambda: run_once(database), float(os.getenv("LLM_POLL_SECONDS", "10")))


if __name__ == "__main__":
    main()
