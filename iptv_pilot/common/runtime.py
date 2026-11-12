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
    result = {name: {"draining": is_draining(database, name),
                   "active": database[collection].count_documents(query)}
            for name, (collection, query) in ACTIVE_TASKS.items()}
    result["background"] = {"draining": is_draining(database, "background"),
                            "active": database.runtime_jobs.count_documents({"active": True})}
    for name in ("web", "push", "scheduler", "epg-engine", "stream-quality", "channel-sync"):
        result.setdefault(name, {"draining": is_draining(database, name),
                                 "active": database.runtime_jobs.count_documents({"_id": name, "active": True})})
    return result


def pipeline_status(database, now=None) -> dict:
    now = now or datetime.now(UTC)
    wait_limit = timedelta(seconds=max(60, int(os.getenv("QUEUE_WAIT_WARNING_SECONDS", "1800"))))
    progress_limit = timedelta(seconds=max(60, int(os.getenv("QUEUE_PROGRESS_WARNING_SECONDS", "1200"))))
    specs = {
        "asr": ({"status": "completed", "asr_enabled": {"$ne": False}},
                {"status": "asr_processing"}, {"status": "asr_failed"},
                "asr_progress_at", "asr_started_at", "asr_completed_at", "next_asr_attempt_at"),
        "encoder": ({"encode_status": "pending", "$or": [
            {"status": {"$in": ["summarized", "asr_failed", "summary_failed"]}},
            {"status": "asr_done", "llm_enabled": False},
            {"status": "completed", "asr_enabled": False}]},
            {"encode_status": "encoding"}, {"encode_status": "failed"},
            "encode_progress_at", "encode_started_at", "encoded_at", "next_encode_attempt_at"),
        "llm": ({"status": "asr_done", "llm_enabled": {"$ne": False}},
                {"status": "summarizing"}, {"status": "summary_failed"},
                "summary_started_at", "summary_started_at", "summary_completed_at", "next_summary_attempt_at"),
    }
    result = {}
    for name, (waiting, active, failed, progress, started, completed, retry) in specs.items():
        pending = list(database.recordings.find(waiting, {"completed_at": 1, retry: 1}))
        ready = [row for row in pending if row.get(retry) is None or
                 (isinstance(row[retry], datetime) and row[retry] <= now)]
        ready_times = [max(row.get("completed_at") or now,
                           row.get(retry) or row.get("completed_at") or now) for row in ready]
        oldest_ready = min(ready_times, default=None)
        oldest_at = min((row.get("completed_at") or now for row in pending), default=None)
        running = list(database.recordings.find(active, {progress: 1, started: 1}))
        stalled_active = any(now - (r.get(progress) or r.get(started) or now) > progress_limit
                             for r in running)
        stalled_waiting = bool(oldest_ready and now-oldest_ready > wait_limit and not running)
        latest = database.recordings.find_one({completed: {"$type": "date"}},
                                              sort=[(completed, -1)]) or {}
        result[name] = {"pending": len(pending), "retrying": len(pending)-len(ready),
                        "active": len(running), "failed": database.recordings.count_documents(failed),
                        "oldest_pending_at": oldest_at, "oldest_ready_at": oldest_ready,
                        "last_success_at": latest.get(completed),
                        "stalled": (stalled_active or stalled_waiting) and not is_draining(database, name),
                        "draining": is_draining(database, name)}
    ready = result["encoder"]["pending"]
    result["encoder"]["blocked"] = max(0, database.recordings.count_documents({"encode_status": "pending"})-ready)
    return result
