from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument

from iptv_pilot.common.db import configure_logging, connect_database, run_forever
from iptv_pilot.llm.providers import create_provider

logger = logging.getLogger(__name__)


def run_once(database) -> None:
    from iptv_pilot.common.runtime import is_draining

    if is_draining(database, "llm"):
        return
    from iptv_pilot.llm.corrections import repair
    from iptv_pilot.llm.live import final_summary, preview_once

    repair(database)

    # Recover a crash between terminal publication and making the push job ready.
    for saved in database.summaries.find({"ready_for_push": False, "outdated": {"$ne": True}}).limit(10):
        if database.recordings.find_one({"_id": saved["recording_id"], "summary_state": "done",
                                         "summary_generation": saved.get("generation"), "analysis_edit_lease": {"$not": {"$gt": datetime.now(UTC)}}}):
            database.summaries.update_one({"_id": saved["_id"], "generation": saved["generation"]},
                                         {"$set": {"ready_for_push": True, "push_processed": False}})
    now = datetime.now(UTC)
    retry_ready = {
        "$or": [
            {"next_summary_attempt_at": None},
            {"next_summary_attempt_at": {"$lte": now}},
        ]
    }
    stale_before = now - timedelta(seconds=int(os.getenv("LLM_STALE_SECONDS", "300")))
    recording = database.recordings.find_one_and_update(
        {
            "llm_enabled": {"$ne": False},
            "analysis_edit_lease": {"$not": {"$gt": now}},
            "$or": [
                {"status": "asr_done", "summary_state": {"$nin": ["done", "failed", "finalizing"]}, **retry_ready},
                {"status": {"$in": ["recording", "completed"]}, "asr_state": "done", "live_state": "done",
                 "summary_state": {"$nin": ["done", "failed", "finalizing"]}, **retry_ready},
                {"$and": [{"$or": [{"status": "summarizing"}, {"summary_state": "finalizing"}]}, {"$or": [
                    {"summary_heartbeat_at": {"$lte": stale_before}},
                    {"summary_heartbeat_at": None, "summary_started_at": {"$lte": stale_before}}]}]},
            ],
        },
        {"$set": {"summary_state": "finalizing",
                  "summary_started_at": now, "summary_heartbeat_at": now,
                  "summary_claim": uuid.uuid4().hex}, "$inc": {"summary_generation": 1}},
        sort=[("start_time", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not recording:
        if os.getenv("LIVE_ASR_ENABLED", "0") == "1":
            preview_once(database, create_provider)
        return
    condition = {"_id": recording["_id"], "summary_state": "finalizing", "summary_claim": recording["summary_claim"]}
    database.recordings.update_one({**condition, "status": {"$ne": "recording"}},
                                   {"$set": {"status": "summarizing"}})
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(20):
            try:
                database.recordings.update_one(condition, {"$set": {"summary_heartbeat_at": datetime.now(UTC)}})
            except Exception:
                logger.warning("summary heartbeat failed")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        transcript = database.transcripts.find_one({"recording_id": recording["_id"]})
        if not transcript:
            raise RuntimeError("transcript not found")
        if not transcript.get("full_text", "").strip():
            raise RuntimeError("transcript is empty")
        if transcript.get("live_epoch") and transcript["live_epoch"] != recording.get("live_epoch"):
            raise RuntimeError("transcript belongs to a previous recording attempt")
        summary = final_summary(database, recording, transcript, create_provider())
        if not database.recordings.find_one(condition, {"_id": 1}):
            return
        document = {
            "recording_id": recording["_id"],
            "transcript_id": transcript["_id"],
            "program_title": recording["program_title"],
            "channel_id": recording["channel_id"],
            "start_time": recording.get("start_time"),
            **summary,
            "pushed": False,
            "push_claim": None, "push_lease_until": None, "push_retry_count": 0, "next_push_attempt_at": None,
            "push_enabled": recording.get("push_after_regenerate", False) if recording.get("summary_regeneration") else recording.get("push_enabled", True),
            "outdated": False,
            # Older push workers understand this flag, so rollback stays safe.
            "push_processed": True,
            "llm_provider": os.getenv("LLM_PROVIDER", "mock"),
            "llm_model": os.getenv("LLM_MODEL", ""),
            "created_at": datetime.now(UTC),
            "generation": recording["summary_generation"], "ready_for_push": False,
        }
        database.summaries.update_one(
            {"recording_id": recording["_id"], "$or": [
                {"generation": {"$lte": recording["summary_generation"]}}, {"generation": None}]},
            {"$set": document}, upsert=True
        )
        published = database.recordings.update_one(
            condition,
            [{"$set": {"status": {"$cond": [{"$eq": ["$status", "recording"]}, "recording", "summarized"]},
                       "next_summary_attempt_at": None, "summary_error": None,
                       "summary_state": "done", "summary_completed_at": datetime.now(UTC)}}],
        )
        if not published.matched_count:
            return
        database.summaries.update_one({"recording_id": recording["_id"], "generation": recording["summary_generation"]},
                                     {"$set": {"ready_for_push": True, "push_processed": False}})
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
            "summary_error": str(exc)[:2000],
            "summary_state": "waiting" if retry_count <= maximum else "failed",
            "status": {"$cond": [{"$eq": ["$status", "recording"]}, "recording",
                                  "asr_done" if retry_count <= maximum else "summary_failed"]},
        }
        if retry_count <= maximum:
            update["next_summary_attempt_at"] = datetime.now(UTC) + timedelta(seconds=30)
        database.recordings.update_one(
            condition,
            [{"$set": update}],
        )
        logger.exception("summary failed recording_id=%s retry=%s/%s", recording["_id"], retry_count, maximum)
    finally:
        stopped.set()
        thread.join(timeout=2)


def main() -> None:
    configure_logging()
    database = connect_database()
    run_forever(lambda: run_once(database), float(os.getenv("LLM_POLL_SECONDS", "10")))


if __name__ == "__main__":
    main()
