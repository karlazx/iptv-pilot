"""Bounded look-ahead plans; only near-term work is enqueued."""

import os
from datetime import timedelta

from iptv_pilot.scheduler.rules import occurrences, resolve_windows


def build_plan(database, now, horizon):
    from iptv_pilot.scheduler.main import matches

    subscriptions = list(database.subscriptions.find({"enabled": True}))
    enabled = set(database.channels.distinct("channel_id", {"enabled": True, "disabled": {"$ne": True},
                                                           "m3u_url": {"$exists": True}}))
    planned, due = {}, set()
    pre = int(os.getenv("RECORDING_PRE_ROLL_SECONDS", "60"))
    post = int(os.getenv("RECORDING_POST_ROLL_SECONDS", "180"))

    def add(subscription, programme, enqueue):
        if programme["channel_id"] not in enabled:
            return
        key = (programme["channel_id"], programme["start_time"])
        value = planned.setdefault(key, {
            "subscription_id": subscription["_id"], "subscription_ids": [],
            "channel_id": programme["channel_id"], "program_title": programme["title"],
            "start_time": programme["start_time"], "end_time": programme["end_time"],
            "scheduled_start": programme["start_time"]-timedelta(seconds=pre),
            "scheduled_end": programme["end_time"]+timedelta(seconds=subscription.get("post_roll_seconds") or post),
            "schedule_kind": programme.get("schedule_kind", "epg"),
            "status": "queued", "retry_count": 0, "created_at": now,
            **{k: subscription.get(k, default) for k, default in (
                ("asr_enabled", True), ("live_asr_enabled", True), ("asr_model", "small"),
                ("llm_enabled", True), ("push_enabled", True), ("auto_bookmark", False))}})
        if subscription["_id"] not in value["subscription_ids"]:
            value["subscription_ids"].append(subscription["_id"])
        value["end_time"] = max(value["end_time"], programme["end_time"])
        value["scheduled_end"] = max(value["scheduled_end"], programme["end_time"]+timedelta(seconds=subscription.get("post_roll_seconds") or post))
        if enqueue:
            due.add(key)

    for sub in subscriptions:
        if sub.get("match_type") == "channel_schedule":
            for event in occurrences(sub, now, now+timedelta(days=1)):
                add(sub, event, event["start_time"]-timedelta(seconds=pre) <= horizon)
    if not subscriptions:
        return [], []
    for event in database.epg_programs.find({"start_time": {"$gte": now, "$lte": horizon}},
                                            {"channel_id": 1, "start_time": 1, "end_time": 1, "title": 1, "description": 1}):
        for sub in subscriptions:
            if matches(sub, event, os.getenv("TZ", "Asia/Shanghai")):
                add(sub, event, True)
    if not due:
        return [], []
    channels = list({key[0] for key in due})
    finish = max(planned[key]["end_time"] for key in due)+timedelta(minutes=10)
    # Look only around channels with imminent work, rather than loading a full
    # day of every channel on each scheduler tick.
    for event in database.epg_programs.find({"channel_id": {"$in": channels},
            "start_time": {"$gte": now, "$lte": finish}},
            {"channel_id": 1, "start_time": 1, "end_time": 1, "title": 1, "description": 1}):
        for sub in subscriptions:
            if matches(sub, event, os.getenv("TZ", "Asia/Shanghai")):
                add(sub, event, False)
    current = list(database.recordings.find({"channel_id": {"$in": channels}, "status": {"$in": ["queued", "recording"]},
        "scheduled_end": {"$gt": now}, "start_time": {"$lte": finish}},
        {"channel_id": 1, "program_title": 1, "start_time": 1, "end_time": 1, "scheduled_start": 1,
         "scheduled_end": 1, "status": 1, "subscription_ids": 1, "requested_start": 1, "requested_end": 1}))
    for row in current:
        planned[(row["channel_id"], row["start_time"])] = row
    resolved = resolve_windows(list(planned.values()))
    return [r for r in resolved if (r["channel_id"], r["start_time"]) in due and "_id" not in r], \
        [r for r in resolved if "_id" in r and r["status"] == "queued"]
