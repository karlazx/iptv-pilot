import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymongo import MongoClient

from iptv_pilot.scheduler.planning import build_plan
from iptv_pilot.scheduler.rules import occurrences, resolve_windows
from iptv_pilot.web.main import SubscriptionCreate
from iptv_pilot.web.pagination import page, setup


@pytest.fixture
def database():
    if not os.getenv("TEST_MONGODB_URI"):
        pytest.skip("TEST_MONGODB_URI required")
    client = MongoClient(os.environ["TEST_MONGODB_URI"], tz_aware=True)
    db = client["iptv_foundation_"+uuid.uuid4().hex]
    db.recordings.create_index([("channel_id", 1), ("start_time", 1)], unique=True)
    setup(db)
    try:
        yield db
    finally:
        client.drop_database(db.name)
        client.close()


def schedule(**rule):
    return {"_id": ObjectId(), "enabled": True, "name": "Overseas news", "match_type": "channel_schedule",
            "channel_id": "overseas", "time_rule": {"time": "19:00", "timezone": "Asia/Shanghai",
            "duration_sec": 3600, **rule}}


def test_schedule_schema_requires_time_duration_and_valid_zone():
    assert SubscriptionCreate.model_validate(schedule()).time_rule.duration_sec == 3600
    for rule in ({"time": None}, {"duration_sec": None}, {"timezone": "Invalid/Zone"}, {"weekdays": []}):
        with pytest.raises(ValidationError):
            SubscriptionCreate.model_validate(schedule(**rule))
    assert SubscriptionCreate(name="legacy", match_type="channel_time", channel_id="c", time_rule={"time": "19:00"})


def test_calendar_schedule_crosses_midnight_without_epg():
    first = datetime(2026, 11, 15, 16, tzinfo=UTC)
    rows = list(occurrences(schedule(time="23:30", duration_sec=7200), first, first+timedelta(hours=2)))
    assert len(rows) == 1 and rows[0]["start_time"] == datetime(2026, 11, 15, 15, 30, tzinfo=UTC)
    assert rows[0]["end_time"]-rows[0]["start_time"] == timedelta(hours=2)


def test_dst_skips_nonexistent_time_and_runs_repeated_time_once():
    sub = schedule(time="01:30", timezone="Europe/London")
    assert list(occurrences(sub, datetime(2026, 3, 29, tzinfo=UTC), datetime(2026, 3, 29, 4, tzinfo=UTC))) == []
    rows = list(occurrences(sub, datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 10, 25, 4, tzinfo=UTC)))
    assert len(rows) == 1 and rows[0]["start_time"].hour == 0


def test_disabled_schedule_does_not_enqueue_and_no_epg_schedule_is_idempotent(database, monkeypatch):
    from iptv_pilot.scheduler import main

    now = datetime(2026, 11, 15, 10, 58, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(main, "datetime", Clock)
    database.channels.insert_one({"channel_id": "overseas", "enabled": True, "m3u_url": "http://example.invalid/live"})
    sub = schedule(weekdays=[6])
    database.subscriptions.insert_one(sub)
    main.schedule_once(database)
    main.schedule_once(database)
    assert database.epg_programs.count_documents({}) == 0
    row = database.recordings.find_one()
    assert database.recordings.count_documents({}) == 1 and row["schedule_kind"] == "channel_schedule"
    assert row["start_time"] == datetime(2026, 11, 15, 11, tzinfo=UTC)
    database.recordings.delete_many({})
    database.subscriptions.update_one({"_id": sub["_id"]}, {"$set": {"enabled": False}})
    main.schedule_once(database)
    assert database.recordings.count_documents({}) == 0


def window(start, seconds, **extra):
    return {"channel_id": "c", "program_title": "programme", "start_time": start,
            "end_time": start+timedelta(seconds=seconds), "scheduled_start": start-timedelta(seconds=60),
            "scheduled_end": start+timedelta(seconds=seconds+180), "status": "queued", **extra}


def test_padding_is_trimmed_without_cutting_programme_and_actual_overlap_is_visible():
    now = datetime.now(UTC)
    rows = resolve_windows([window(now, 60), window(now+timedelta(seconds=60), 3600)])
    assert rows[0]["scheduled_end"] == rows[1]["scheduled_start"] == now+timedelta(seconds=60)
    assert all(r["padding_trimmed"] for r in rows)
    rows = resolve_windows([window(now, 3600), window(now+timedelta(minutes=30), 3600)])
    assert rows[1]["window_conflicts"][0]["reason"] == "programme_overlap"
    assert rows[0]["scheduled_end"] == now+timedelta(seconds=3780)
    rows = resolve_windows([window(now, 60, status="recording"), window(now+timedelta(seconds=60), 3600)])
    assert rows[1]["window_conflicts"][0]["reason"] == "active_capture_padding"


def test_planner_forecasts_next_subscribed_programme_before_first_capture(database):
    now = datetime.now(UTC).replace(microsecond=0)
    database.channels.insert_one({"channel_id": "c", "enabled": True, "m3u_url": "http://example.invalid/live"})
    database.subscriptions.insert_one({"name": "news", "match_type": "keyword", "keyword": "news", "enabled": True})
    start = now+timedelta(minutes=5)
    database.epg_programs.insert_many([{"channel_id": "c", "title": "news", "start_time": start,
                                      "end_time": start+timedelta(minutes=30)},
                                     {"channel_id": "c", "title": "news next", "start_time": start+timedelta(minutes=30),
                                      "end_time": start+timedelta(minutes=60)}])
    due, _ = build_plan(database, now, now+timedelta(minutes=15))
    assert len(due) == 1 and due[0]["scheduled_end"] == start+timedelta(minutes=30)


def test_disabled_subscription_cannot_start_an_already_queued_capture(database, monkeypatch):
    from iptv_pilot.recorder import main as recorder

    sid = database.subscriptions.insert_one({"enabled": False}).inserted_id
    r = window(datetime.now(UTC), 3600)
    r.update(claim_id="claim", subscription_ids=[sid])
    r["_id"] = database.recordings.insert_one(r).inserted_id
    monkeypatch.setattr(recorder, "capture_stream", lambda *args, **kwargs: pytest.fail("disabled rule captured"))
    recorder.process_recording(database, r)
    assert database.recordings.find_one({"_id": r["_id"]})["status"] == "cancelled"


def test_pre_roll_lateness_is_not_programme_loss_and_saved_head_survives_retries(database):
    from iptv_pilot.recorder.main import _mark_partial_if_late

    now = datetime.now(UTC)
    r = window(now+timedelta(seconds=30), 3600)
    r.update(scheduled_start=now-timedelta(minutes=10), claim_id="claim")
    r["_id"] = database.recordings.insert_one(r).inserted_id
    _mark_partial_if_late(database, r, now, "claim")
    assert r["schedule_late_seconds"] == 600 and r["missing_head_seconds"] == 0 and not r["partial"]
    r["recording_parts"] = [{"started_at": r["start_time"]}]
    _mark_partial_if_late(database, r, now+timedelta(minutes=20), "claim")
    assert r["partial"] and r["missing_head_seconds"] == 0


def test_calendar_recording_does_not_claim_epg_timing_is_wrong(monkeypatch, tmp_path):
    from iptv_pilot.recorder import main as recorder

    monkeypatch.setattr(recorder, "probe_media_duration", lambda *a: pytest.fail("no EPG comparison expected"))
    assert not recorder._epg_deviation_suspect({"schedule_kind": "channel_schedule"}, tmp_path/"video.ts")


def test_cursor_reads_every_row_including_ties_and_rejects_changed_filters(database):
    when = datetime.now(UTC)
    database.recordings.insert_many([{"channel_id": f"c{i}", "program_title": "[literal]", "status": "summarized",
                                     "start_time": when+timedelta(seconds=i//3)} for i in range(205)])
    first = page(database, "recordings", limit=50, q="[literal]")
    found = [r["_id"] for r in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        following = page(database, "recordings", limit=50, cursor=cursor, q="[literal]")
        found += [r["_id"] for r in following["items"]]
        cursor = following["next_cursor"]
    assert len(found) == len(set(found)) == 205
    with pytest.raises(Exception) as error:
        page(database, "recordings", cursor=first["next_cursor"], q="different")
    assert error.value.status_code == 400
    assert page(database, "recordings", q="[absent]")["items"] == []


def test_bookmark_search_does_not_truncate_matching_content(database):
    ids = database.recordings.insert_many([{"channel_id": f"c{i}", "program_title": "saved title",
                                          "start_time": datetime.now(UTC)} for i in range(1005)]).inserted_ids
    database.bookmarks.insert_one({"user": "default", "item_type": "recording", "item_id": ids[-1], "note": ""})
    assert len(page(database, "bookmarks", q="saved")["items"]) == 1


def test_list_apis_preview_status_lookup_and_legacy_contract(database, monkeypatch):
    from iptv_pilot.web import main as web

    monkeypatch.setattr(web, "database", database)
    client = TestClient(web.app)
    rid = database.recordings.insert_one({"channel_id": "c", "program_title": "news", "status": "queued",
                                          "start_time": datetime.now(UTC)}).inserted_id
    assert isinstance(client.get("/api/recordings").json(), list)
    result = client.get("/api/library/recordings?q=news&limit=1").json()
    assert result["items"][0]["_id"] == str(rid) and not result["has_more"]
    assert client.get("/api/library/recordings?from=2026-11-01T00:00:00").status_code == 422
    assert client.get("/api/library/recordings?cursor=invalid").status_code == 400
    assert client.get("/api/library/recordings?limit=101").status_code == 422
    assert client.get("/api/library/unknown").status_code == 404
    assert client.post("/api/recordings/status", json={"ids": [str(rid)]}).json()[0]["status"] == "queued"
    assert client.post("/api/recordings/status", json={"ids": [str(rid)]*101}).status_code == 422
    preview = client.post("/api/subscriptions/preview", json={k:v for k,v in schedule().items() if k!="_id"})
    assert preview.status_code == 200 and preview.json()["items"]
    assert database.subscriptions.count_documents({}) == 0


def test_private_project_area_is_not_tracked_or_sent_as_build_context():
    assert ".deploy-history/" in Path(".dockerignore").read_text(encoding="utf-8")
    if Path(".git").exists():
        assert not subprocess.check_output(["git", "ls-files", ".deploy-history"], text=True).strip()
