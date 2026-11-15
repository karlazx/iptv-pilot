"""Calendar schedules and padding-only conflict resolution."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def occurrences(subscription, first, last):
    rule = subscription.get("time_rule") or {}
    zone = ZoneInfo(rule.get("timezone", "Asia/Shanghai"))
    hour, minute = map(int, rule["time"].split(":"))
    duration = timedelta(seconds=rule["duration_sec"])
    day = (first-duration-timedelta(days=1)).astimezone(zone).date()
    finish = (last+timedelta(days=1)).astimezone(zone).date()
    weekdays = rule.get("weekdays")
    if weekdays is None:
        weekdays = [rule["weekday"]] if rule.get("weekday") is not None else list(range(7))
    while day <= finish:
        if day.weekday() in weekdays:
            naive = datetime(day.year, day.month, day.day, hour, minute)
            start = naive.replace(tzinfo=zone, fold=0).astimezone(UTC)
            # A spring-forward clock time may not exist. Fall-back executes
            # the first occurrence only, rather than recording twice.
            if start.astimezone(zone).replace(tzinfo=None) == naive and start+duration > first and start <= last:
                yield {"channel_id": subscription["channel_id"], "title": subscription["name"],
                       "start_time": start, "end_time": start+duration, "schedule_kind": "channel_schedule"}
        day += timedelta(days=1)


def resolve_windows(rows):
    """Trim only padding; report actual programme overlap without deleting work."""
    rows = sorted(rows, key=lambda r: (r["channel_id"], r["start_time"]))
    for row in rows:
        row.setdefault("requested_start", row["scheduled_start"])
        row.setdefault("requested_end", row["scheduled_end"])
        row["window_conflicts"] = []
        if row.get("status") != "recording":
            row.update(scheduled_start=row["requested_start"], scheduled_end=row["requested_end"], padding_trimmed=False)
    for index, later in enumerate(rows):
        for earlier in rows[:index]:
            if earlier["channel_id"] != later["channel_id"] or earlier["scheduled_end"] <= later["scheduled_start"]:
                continue
            if earlier["end_time"] > later["start_time"] or earlier.get("status") == "recording":
                reason = "programme_overlap" if earlier["end_time"] > later["start_time"] else "active_capture_padding"
                later.setdefault("window_conflicts", []).append({"reason": reason, "start": earlier["start_time"],
                                                                 "title": earlier["program_title"]})
                continue
            boundary = min(later["start_time"], max(earlier["end_time"], later["scheduled_start"]))
            earlier["scheduled_end"] = min(earlier["scheduled_end"], boundary)
            later["scheduled_start"] = max(later["scheduled_start"], boundary)
            earlier["padding_trimmed"] = later["padding_trimmed"] = True
    for row in rows:
        row["window_seconds"] = (row["scheduled_end"]-row["scheduled_start"]).total_seconds()
    return rows
