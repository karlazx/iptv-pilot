import os
import uuid
import wave
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import MongoClient

from iptv_pilot.asr import main as asr


@pytest.fixture
def database():
    uri = os.getenv("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("TEST_MONGODB_URI is required")
    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=5000)
    db = client[f"iptv_pilot_test_asr_{uuid.uuid4().hex}"]
    db.transcripts.create_index("recording_id", unique=True)
    try:
        yield db
    finally:
        client.drop_database(db.name)
        client.close()


def queued(database, **values):
    return database.recordings.insert_one({
        "status": "completed", "asr_enabled": True, "completed_at": datetime.now(UTC),
        "channel_id": "test", "program_title": "test", "start_time": datetime.now(UTC),
        "file_path": "/recordings/test.wav", **values,
    }).inserted_id


def expire(database, identifier):
    database.recordings.update_one({"_id": identifier}, {"$set": {
        "asr_heartbeat_at": datetime.now(UTC) - timedelta(days=1),
    }})


def test_crashes_consume_budget_and_allow_downstream_encoding(database, monkeypatch):
    monkeypatch.setenv("ASR_MAX_RETRIES", "1")
    rid = queued(database, encode_status="pending", next_encode_attempt_at=None)
    first = asr.claim_job(database)
    assert first["asr_attempt_count"] == 1
    assert asr.claim_job(database) is None
    expire(database, rid)
    second = asr.claim_job(database)
    assert second["asr_attempt_count"] == 2
    with pytest.raises(asr.ClaimLost):
        asr.owned_update(database, first, {"status": "asr_done"})
    expire(database, rid)
    assert asr.claim_job(database) is None
    assert database.recordings.find_one({"_id": rid})["status"] == "asr_failed"
    from iptv_pilot.recorder.encoder import claim_job

    assert claim_job(database)["_id"] == rid


def test_null_retry_is_ready_but_future_retry_is_not(database):
    queued(database, next_asr_attempt_at=datetime.now(UTC) + timedelta(hours=1))
    assert asr.claim_job(database) is None
    rid = queued(database, next_asr_attempt_at=None)
    assert asr.claim_job(database)["_id"] == rid


def test_recent_heartbeat_protects_a_long_running_job(database):
    queued(database, status="asr_processing", asr_started_at=datetime.now(UTC)-timedelta(days=1),
           asr_heartbeat_at=datetime.now(UTC), asr_attempt_count=1)
    assert asr.claim_job(database) is None


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000 * 65)
    return path


def test_audio_windows_are_bounded_and_preserve_tail(audio_file):
    pytest.importorskip("av")
    windows = list(asr.audio_windows(str(audio_file), 30))
    assert [offset for offset, _ in windows] == [0, 30, 60]
    assert [len(audio) for _, audio in windows] == [480000, 480000, 80000]
    assert all(str(audio.dtype) == "float32" for _, audio in windows)


def test_retry_resumes_saved_chunks(database, audio_file, monkeypatch):
    pytest.importorskip("av")
    monkeypatch.setenv("ASR_BACKEND", "faster_whisper")
    monkeypatch.setenv("ASR_CHUNK_SECONDS", "30")
    rid = queued(database, file_path=str(audio_file))
    calls = []

    def fail_second(audio, model):
        calls.append(len(audio))
        if len(calls) == 2:
            raise RuntimeError("simulated inference interruption")
        return [{"start": 0, "end": 1, "text": "first"}]

    monkeypatch.setattr(asr, "transcribe_window", fail_second)
    monkeypatch.setattr(asr, "runtime_info", lambda: {"engine": "openvino", "device": "GPU"})
    asr.process_recording(database, asr.claim_job(database))
    assert database.asr_chunks.count_documents({"recording_id": rid}) == 1
    database.recordings.update_one({"_id": rid}, {"$set": {"next_asr_attempt_at": None}})
    resumed = []

    def succeed(audio, model):
        resumed.append(len(audio))
        return [{"start": 0, "end": 1, "text": "rest"}]

    monkeypatch.setattr(asr, "transcribe_window", succeed)
    monkeypatch.setattr(asr, "runtime_info", lambda: {"engine": "faster_whisper", "device": "cpu"})
    asr.process_recording(database, asr.claim_job(database))
    assert resumed == [480000, 80000]
    assert database.recordings.find_one({"_id": rid})["status"] == "asr_done"
    transcript = database.transcripts.find_one({"recording_id": rid})
    assert [s["start"] for s in transcript["segments"]] == [0, 30, 60]
    assert transcript["asr_runtimes"] == [
        {"engine": "openvino", "device": "GPU"},
        {"engine": "faster_whisper", "device": "cpu"},
    ]
    assert transcript["asr_runtime"]["device"] == "cpu"
    assert database.asr_chunks.count_documents({"recording_id": rid}) == 0


def test_queue_health_distinguishes_blocked_encoding_from_a_stall(database):
    from iptv_pilot.common.runtime import pipeline_status

    queued(database, status="asr_processing", encode_status="pending",
           asr_started_at=datetime.now(UTC) - timedelta(hours=1))
    status = pipeline_status(database)
    assert status["asr"]["stalled"] is True
    assert status["encoder"]["blocked"] == 1
    assert status["encoder"]["stalled"] is False


def test_queue_health_warns_when_ready_work_is_not_claimed(database):
    from iptv_pilot.common.runtime import pipeline_status

    queued(database, completed_at=datetime.now(UTC) - timedelta(hours=1))
    assert pipeline_status(database)["asr"]["stalled"] is True


@pytest.mark.parametrize("stage,status,retry", [
    ("asr", "completed", "next_asr_attempt_at"),
    ("encoder", "summarized", "next_encode_attempt_at"),
    ("llm", "asr_done", "next_summary_attempt_at"),
])
@pytest.mark.parametrize("delay", [600, -5])
def test_queue_health_respects_backoff_and_when_work_became_ready(
    database, stage, status, retry, delay
):
    from iptv_pilot.common.runtime import pipeline_status

    now = datetime.now(UTC)
    queued(database, status=status, completed_at=now-timedelta(hours=2),
           encode_status="pending", **{retry: now+timedelta(seconds=delay)})
    result = pipeline_status(database, now)[stage]
    assert result["pending"] == 1
    assert result["retrying"] == (1 if delay > 0 else 0)
    assert result["stalled"] is False
