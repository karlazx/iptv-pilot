import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import MongoClient

from iptv_pilot.asr import live
from iptv_pilot.llm import main as llm
from iptv_pilot.llm.live import preview_once, reduce_text
from iptv_pilot.llm.providers import MockProvider


def test_context_windows_have_contiguous_commit_ranges_without_losing_audio():
    import numpy as np

    from iptv_pilot.recorder.live_audio import ContextSegmenter

    pcm = np.concatenate([np.full(25*16000, 1000, dtype="<i2"),
                          np.zeros(16000, dtype="<i2"), np.full(30*16000, 1000, dtype="<i2")]).tobytes()
    splitter = ContextSegmenter(30)
    pieces = []
    for i in range(0, len(pcm), 32000):
        pieces.extend(splitter.feed(pcm[i:i+32000]))
    pieces.extend(splitter.feed(b"", final=True))
    restored = []
    for piece in pieces:
        left = round((piece["offset"]-piece["audio_offset"])*32000)
        restored.append(piece["pcm"][left:left+round(piece["duration"]*32000)])
    assert b"".join(restored) == pcm
    assert len(pieces[0]["pcm"])/32000 == 25
    assert all(len(p["pcm"])/32000 <= 30 for p in pieces)


def test_verified_windows_preserve_offline_audio_and_flush_at_programme_end():
    from iptv_pilot.recorder.live_audio import VerifiedSegmenter

    pcm = bytes(range(256))*80000
    splitter = VerifiedSegmenter(300, upper=350)
    pieces = []
    for i in range(0, len(pcm), 32000):
        pieces.extend(splitter.feed(pcm[i:i+32000]))
    pieces.extend(splitter.feed(b"", final=True))
    assert [p["duration"] for p in pieces] == [300, 50]
    assert b"".join(p["pcm"] for p in pieces) == pcm[:350*32000]
    assert pieces[1]["offset"] == 300


@pytest.fixture
def database():
    if not os.getenv("TEST_MONGODB_URI"):
        pytest.skip("TEST_MONGODB_URI required")
    client = MongoClient(os.environ["TEST_MONGODB_URI"], tz_aware=True)
    db = client[f"iptv_live_test_{uuid.uuid4().hex}"]
    live.setup(db)
    db.transcripts.create_index("recording_id", unique=True)
    db.summaries.create_index("recording_id", unique=True)
    try:
        yield db
    finally:
        client.drop_database(db.name)
        client.close()


def recording(database, **extra):
    now = datetime.now(UTC)
    row = {"channel_id": "test", "program_title": "test", "status": "recording",
           "start_time": now-timedelta(seconds=90), "capture_started_at": now-timedelta(seconds=90),
           "end_time": now-timedelta(seconds=1), "scheduled_end": now+timedelta(seconds=90),
           "live_epoch": "epoch", "live_state": "streaming", "asr_state": "streaming",
           "summary_state": "waiting", "live_chunk_count": 3, "live_verified_count": 3, "live_transcribed_seconds": 90,
           "push_enabled": False, **extra}
    row["_id"] = database.recordings.insert_one(row).inserted_id
    return row


def done_chunks(database, row, n=3):
    for lane in ("draft", "verified"):
        for i in range(n):
            database.live_chunks.insert_one({"_id": f"{row['_id']}:{row['live_epoch']}:{lane}:{i}", "lane": lane,
                "recording_id": row["_id"], "epoch": row["live_epoch"], "seq": i, "state": "done",
                "duration": 30, "offset": i*30, "created_at": datetime.now(UTC),
                "segments": [{"start": i*30, "end": (i+1)*30, "text": f"section {i}"}],
                "runtime": {"device": "GPU"}})


def test_programme_finalizes_while_capture_keeps_postroll(database, monkeypatch):
    row = recording(database)
    done_chunks(database, row)
    live.finalize(database)
    current = database.recordings.find_one({"_id": row["_id"]})
    assert current["status"] == "recording" and current["asr_state"] == "done"
    monkeypatch.setattr(llm, "create_provider", MockProvider)
    llm.run_once(database)
    current = database.recordings.find_one({"_id": row["_id"]})
    assert current["status"] == "recording" and current["summary_state"] == "done"
    assert database.summaries.find_one({"recording_id": row["_id"]})["ready_for_push"] is True
    llm.run_once(database)
    assert database.summaries.count_documents({}) == 1
    assert database.recordings.find_one({"_id": row["_id"]})["summary_generation"] == 1


def test_live_final_waits_for_missing_middle_chunk(database):
    row = recording(database)
    done_chunks(database, row)
    database.live_chunks.update_one({"recording_id": row["_id"], "lane": "verified", "seq": 1}, {"$set": {"state": "ready"}})
    live.finalize(database)
    assert database.transcripts.count_documents({}) == 0
    assert database.recordings.find_one({"_id": row["_id"]})["status"] == "recording"


def test_draft_errors_cannot_feed_final_transcript_or_summary(database, monkeypatch):
    row = recording(database)
    done_chunks(database, row)
    database.live_chunks.update_many({"lane": "draft"}, {"$set": {"segments": [
        {"start": 0, "end": 90, "text": "WRONG DRAFT FACT"}]}})
    live.finalize(database)
    transcript = database.transcripts.find_one({"recording_id": row["_id"]})
    assert "WRONG" not in transcript["full_text"]
    monkeypatch.setattr(llm, "create_provider", MockProvider)
    llm.run_once(database)
    assert "WRONG" not in database.summaries.find_one({"recording_id": row["_id"]})["summary_text"]


def test_programme_end_allows_audio_sample_rounding(database):
    capture = datetime.now(UTC)-timedelta(seconds=100)
    row = recording(database, capture_started_at=capture, end_time=capture+timedelta(seconds=90.00003))
    done_chunks(database, row)
    live.finalize(database)
    assert database.recordings.find_one({"_id": row["_id"]})["asr_state"] == "done"


def test_push_claim_blocks_concurrent_delivery(database, monkeypatch):
    from iptv_pilot.push import main as push

    calls = []

    class Provider:
        name = "test"

        def push_summary(self, summary):
            calls.append(summary["_id"])
            push.run_once(database)
            return True

    monkeypatch.setattr(push, "load_config", lambda *a: {"channels": ["test"]})
    monkeypatch.setattr(push, "create_providers", lambda *a: [Provider()])
    monkeypatch.setattr(push, "deliver", lambda p, s: p.push_summary(s))
    database.summaries.insert_one({"push_enabled": True, "ready_for_push": True,
                                   "created_at": datetime.now(UTC)})
    push.run_once(database)
    assert len(calls) == 1


def test_preview_never_publishes_push_document(database, monkeypatch):
    monkeypatch.setenv("LIVE_SUMMARY_INTERVAL_SECONDS", "60")
    row = recording(database, end_time=datetime.now(UTC)+timedelta(minutes=10))
    done_chunks(database, row)
    preview_once(database, MockProvider)
    current = database.recordings.find_one({"_id": row["_id"]})
    assert current["live_preview"]["draft"] is True
    assert database.summaries.count_documents({}) == 0
    assert current["status"] == "recording"


def test_old_preview_cannot_replace_new_capture_epoch(database, monkeypatch):
    monkeypatch.setenv("LIVE_SUMMARY_INTERVAL_SECONDS", "60")
    row = recording(database, end_time=datetime.now(UTC)+timedelta(minutes=10))
    done_chunks(database, row)

    class Replaced(MockProvider):
        def summarize(self, text):
            database.recordings.update_one({"_id": row["_id"]}, {"$set": {"live_epoch": "new"}})
            return super().summarize(text)

    preview_once(database, Replaced)
    assert not database.recordings.find_one({"_id": row["_id"]}).get("live_preview")


def test_expired_crash_budget_returns_to_offline(database):
    row = recording(database)
    database.live_chunks.insert_one({"_id": "crashed", "recording_id": row["_id"],
        "epoch": row["live_epoch"], "seq": 0, "attempts": 3, "state": "processing",
        "lease_until": datetime.now(UTC)-timedelta(seconds=1), "created_at": datetime.now(UTC)})
    assert live.run_once(database) is False
    assert database.recordings.find_one({"_id": row["_id"]})["live_state"] == "fallback"


def test_stale_epoch_job_is_cancelled_without_inference(database, monkeypatch):
    row = recording(database, live_epoch="new")
    database.live_chunks.insert_one({"_id": "stale", "recording_id": row["_id"],
        "epoch": "old", "seq": 0, "attempts": 0, "state": "ready", "path": "/not-a-cache-file",
        "created_at": datetime.now(UTC)})
    monkeypatch.setattr(live, "get_session", lambda *a: pytest.fail("stale input inferred"))
    assert live.run_once(database)
    assert database.live_chunks.find_one({"_id": "stale"})["state"] == "cancelled"


def test_long_summary_inputs_include_the_tail():
    calls = []

    class Provider(MockProvider):
        def summarize(self, text):
            calls.append(text)
            return {"summary_text": text[-24:], "key_points": [], "entities": [], "follow_up": ""}

    reduce_text(Provider(), "A"*1600+"FINAL-FACT", 512)
    assert any("FINAL-FACT" in text for text in calls)
    assert all(len(text) <= 512 for text in calls)


def test_final_reuses_already_persisted_incremental_notes(database, monkeypatch):
    monkeypatch.setenv("LIVE_SUMMARY_INTERVAL_SECONDS", "60")
    row = recording(database)
    done_chunks(database, row)
    calls = []

    class Provider(MockProvider):
        def summarize(self, text):
            calls.append(text)
            return super().summarize(text)

    preview_once(database, Provider)
    assert len(calls) == 2
    live.finalize(database)
    monkeypatch.setattr(llm, "create_provider", Provider)
    llm.run_once(database)
    assert len(calls) == 4  # draft + verified cached minute + verified tail + final


def test_final_owner_loss_cannot_publish_or_push(database, monkeypatch):
    row = recording(database, status="asr_done", asr_state="done", live_state="done")
    database.transcripts.insert_one({"recording_id": row["_id"], "full_text": "original"})

    class Replaced(MockProvider):
        def summarize(self, text):
            database.recordings.update_one({"_id": row["_id"]}, {"$set": {
                "summary_claim": "replacement", "summary_generation": 2}})
            return super().summarize(text)

    monkeypatch.setattr(llm, "create_provider", Replaced)
    llm.run_once(database)
    assert database.summaries.count_documents({}) == 0


def test_retry_does_not_redeliver_successful_push_channel(database, monkeypatch):
    from iptv_pilot.push import main as push

    calls = []

    class Provider:
        def __init__(self, name):
            self.name = name

        def push_summary(self, summary):
            calls.append(self.name)
            if self.name == "second" and calls.count("second") == 1:
                raise RuntimeError("transient provider failure")
            return True

    monkeypatch.setattr(push, "load_config", lambda *a: {"channels": ["first", "second"]})
    monkeypatch.setattr(push, "create_providers", lambda *a: [Provider("first"), Provider("second")])
    monkeypatch.setattr(push, "deliver", lambda p, s: p.push_summary(s))
    sid = database.summaries.insert_one({"push_enabled": True, "ready_for_push": True,
                                         "created_at": datetime.now(UTC)}).inserted_id
    push.run_once(database)
    assert database.push_events.find_one({"summary_id": sid, "provider": "first"})["status"] == "delivered"
    database.summaries.update_one({"_id": sid}, {"$set": {"next_push_attempt_at": None}})
    push.run_once(database)
    assert calls == ["first", "second", "second"]
    assert database.summaries.find_one({"_id": sid})["push_processed"] is True


@pytest.mark.parametrize("state", ["fallback", "done"])
def test_sidecar_finish_does_not_override_asr_state(database, state):
    from iptv_pilot.recorder.live_audio import LiveAudio

    row = recording(database, claim_id="epoch", live_state=state)
    observer = LiveAudio(database, row, None, None)
    observer.thread.start = lambda: None
    observer.thread.join = lambda **kw: None
    observer.thread.is_alive = lambda: False
    observer.finish(True)
    assert database.recordings.find_one({"_id": row["_id"]})["live_state"] == state
