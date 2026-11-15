from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

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
    from iptv_pilot.scheduler.planning import build_plan

    now = datetime.now(UTC)
    horizon = now + timedelta(minutes=max(1, int(os.getenv("SCHEDULER_HORIZON_MINUTES", "15"))))
    planned, queued = build_plan(database, now, horizon)
    operations = []
    for document in planned:
        ids = document.pop("subscription_ids")
        operations.append(UpdateOne({"channel_id": document["channel_id"], "start_time": document["start_time"]},
            {"$setOnInsert": document, "$addToSet": {"subscription_ids": {"$each": ids}}}, upsert=True))
    for document in queued:
        values = {k: document[k] for k in ("scheduled_start", "scheduled_end", "window_seconds", "requested_start", "requested_end")}
        values.update(padding_trimmed=document.get("padding_trimmed", False), window_conflicts=document.get("window_conflicts", []))
        operations.append(UpdateOne({"_id": document["_id"], "status": "queued"}, {"$set": values}))
    if operations:
        try:
            result = database.recordings.bulk_write(operations, ordered=False)
        except BulkWriteError:
            logger.exception("schedule write failed; verify recording uniqueness indexes")
            return
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
