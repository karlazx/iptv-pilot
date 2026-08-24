from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from pymongo import UpdateOne

from iptv_pilot.common.db import configure_logging, connect_database

logger = logging.getLogger(__name__)


def matches(subscription: dict, programme: dict, timezone_name: str = "Asia/Shanghai") -> bool:
    match_type = subscription.get("match_type")
    if match_type == "keyword":
        keyword = subscription.get("keyword", "").strip()
        if subscription.get("channel_id") and subscription["channel_id"] != programme.get("channel_id"):
            return False
        haystack = "\n".join(
            [programme.get("title", ""), programme.get("description", "")]
        )
        return bool(keyword and re.search(re.escape(keyword), haystack, re.I))
    if match_type == "channel_time":
        if subscription.get("channel_id") != programme.get("channel_id"):
            return False
        rule = subscription.get("time_rule") or {}
        local_tz = ZoneInfo(rule.get("timezone") or timezone_name)
        start = programme["start_time"].astimezone(local_tz)
        weekdays = rule.get("weekdays")
        if weekdays is None and rule.get("weekday") is not None:
            weekdays = [rule["weekday"]]
        expected_time = rule.get("time")
        if weekdays is not None and start.weekday() not in weekdays:
            return False
        if not expected_time:
            return True
        try:
            hour, minute = (int(value) for value in expected_time.split(":", 1))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid channel_time rule: {expected_time!r}") from exc
        actual_minutes = start.hour * 60 + start.minute
        expected_minutes = hour * 60 + minute
        difference = abs(actual_minutes - expected_minutes)
        difference = min(difference, 1440 - difference)
        return difference <= int(rule.get("tolerance_min", 0))
    return False


def schedule_once(database) -> None:
    now = datetime.now(UTC)
    horizon = now + timedelta(minutes=max(1, int(os.getenv("SCHEDULER_HORIZON_MINUTES", "15"))))
    subscriptions = list(database.subscriptions.find({"enabled": True}))
    if not subscriptions:
        return
    programmes = list(
        database.epg_programs.find({"start_time": {"$gte": now, "$lte": horizon}})
    )
    enabled_channels = set(database.channels.distinct("channel_id", {"enabled": True, "disabled": {"$ne": True}, "m3u_url": {"$exists": True}}))
    programmes = [row for row in programmes if row.get("channel_id") in enabled_channels]
    pre_roll = int(os.getenv("RECORDING_PRE_ROLL_SECONDS", "60"))
    default_post_roll = int(os.getenv("RECORDING_POST_ROLL_SECONDS", "180"))
    operations: list[UpdateOne] = []
    for subscription in subscriptions:
        post_roll = int(subscription.get("post_roll_seconds") or default_post_roll)
        for programme in programmes:
            if not matches(subscription, programme, os.getenv("TZ", "Asia/Shanghai")):
                continue
            operations.append(
                UpdateOne(
                    {
                        "subscription_id": subscription["_id"],
                        "start_time": programme["start_time"],
                    },
                    {
                        "$setOnInsert": {
                            "subscription_id": subscription["_id"],
                            "channel_id": programme["channel_id"],
                            "program_title": programme["title"],
                            "start_time": programme["start_time"],
                            "end_time": programme["end_time"],
                            "scheduled_start": programme["start_time"] - timedelta(seconds=pre_roll),
                            "scheduled_end": programme["end_time"] + timedelta(seconds=post_roll),
                            "status": "queued",
                            "retry_count": 0,
                            "created_at": now,
                            "asr_enabled": subscription.get("asr_enabled", True),
                            "asr_model": subscription.get("asr_model", "small"),
                            "llm_enabled": subscription.get("llm_enabled", True),
                            "push_enabled": subscription.get("push_enabled", True),
                            "auto_bookmark": subscription.get("auto_bookmark", False),
                        }
                    },
                    upsert=True,
                )
            )
    if operations:
        result = database.recordings.bulk_write(operations, ordered=False)
        logger.info("scheduled recordings upserted=%s", result.upserted_count)


def main() -> None:
    configure_logging()
    database = connect_database()
    interval = max(10, int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60")))
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        schedule_once,
        "interval",
        seconds=interval,
        args=[database],
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )
    logger.info("scheduler started interval_seconds=%s", interval)
    scheduler.start()


if __name__ == "__main__":
    main()
