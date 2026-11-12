"""Shared maintenance controls and queue-level health (not just process liveness)."""

import os
from datetime import UTC, datetime, timedelta

ACTIVE_TASKS = {
    "recorder": ("recordings", {"status": "recording"}),
    "encoder": ("recordings", {"encode_status": "encoding"}),
    "asr": ("recordings", {"status": "asr_processing"}),
    "llm": ("recordings", {"status": "summarizing"}),
}


def is_draining(database, service: str) -> bool:
    return bool(database.settings.find_one({"key": f"drain:{service}", "value": True}))


def maintenance_status(database) -> dict:
    return {name: {"draining": is_draining(database, name),
                   "active": database[collection].count_documents(query)}
            for name, (collection, query) in ACTIVE_TASKS.items()}


def pipeline_status(database, now=None) -> dict:
    now = now or datetime.now(UTC)
    wait_limit = timedelta(seconds=max(60, int(os.getenv("QUEUE_WAIT_WARNING_SECONDS", "1800"))))
    progress_limit = timedelta(seconds=max(60, int(os.getenv("QUEUE_PROGRESS_WARNING_SECONDS", "1200"))))
    specs = {
        "asr": ({"status": "completed", "asr_enabled": {"$ne": False}},
                {"status": "asr_processing"}, {"status": "asr_failed"},
                "asr_progress_at", "asr_started_at", "asr_completed_at"),
        "encoder": ({"encode_status": "pending", "$or": [
            {"status": {"$in": ["summarized", "asr_failed", "summary_failed"]}},
            {"status": "asr_done", "llm_enabled": False},
            {"status": "completed", "asr_enabled": False}]},
            {"encode_status": "encoding"}, {"encode_status": "failed"},
            "encode_heartbeat_at", "encode_started_at", "encoded_at"),
        "llm": ({"status": "asr_done", "llm_enabled": {"$ne": False}},
                {"status": "summarizing"}, {"status": "summary_failed"},
                "summary_started_at", "summary_started_at", "summary_completed_at"),
    }
    result = {}
    for name, (waiting, active, failed, progress, started, completed) in specs.items():
        oldest = database.recordings.find_one(waiting, sort=[("completed_at", 1)]) or {}
        oldest_at = oldest.get("completed_at")
        running = list(database.recordings.find(active, {progress: 1, started: 1}))
        stalled_active = any(now - (r.get(progress) or r.get(started) or now) > progress_limit
                             for r in running)
        stalled_waiting = bool(oldest_at and now-oldest_at > wait_limit and not running)
        latest = database.recordings.find_one({completed: {"$type": "date"}},
                                              sort=[(completed, -1)]) or {}
        result[name] = {"pending": database.recordings.count_documents(waiting),
                        "active": len(running), "failed": database.recordings.count_documents(failed),
                        "oldest_pending_at": oldest_at, "last_success_at": latest.get(completed),
                        "stalled": stalled_active or stalled_waiting,
                        "draining": is_draining(database, name)}
    ready = result["encoder"]["pending"]
    result["encoder"]["blocked"] = database.recordings.count_documents({"encode_status": "pending"})-ready
    return result
