import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pymongo import MongoClient

from iptv_pilot.common.media_refs import (
    MediaBusy,
    MediaReader,
    key,
    reap_originals,
    retire_file,
    retire_files,
)
from iptv_pilot.llm import corrections, traceable
from iptv_pilot.llm.providers import MockProvider
from iptv_pilot.recorder import main as recorder
from iptv_pilot.recorder.partials import coverage, report_source, save_partial


@pytest.fixture
def database():
    if not os.getenv("TEST_MONGODB_URI"):
        pytest.skip("TEST_MONGODB_URI required")
    client = MongoClient(os.environ["TEST_MONGODB_URI"], tz_aware=True)
    db = client["iptv_phase2_test_"+uuid.uuid4().hex]
    db.transcripts.create_index("recording_id", unique=True)
    db.summaries.create_index("recording_id", unique=True)
    try:
        yield db
    finally:
        client.drop_database(db.name)
        client.close()


def row(database, **extra):
    now = datetime.now(UTC)
    value = {"status": "summarized", "asr_state": "done", "summary_state": "done",
             "summary_generation": 1, "channel_id": "test", "program_title": "test",
             "start_time": now, "end_time": now+timedelta(seconds=600), "timeline_origin": now,
             "claim_id": "attempt", "push_enabled": True, **extra}
    value["_id"] = database.recordings.insert_one(value).inserted_id
    return value


def test_bundled_ffmpeg_isolates_gconv_without_changing_operator_configuration(monkeypatch):
    from iptv_pilot.common.ffmpeg import ffmpeg_environment

    monkeypatch.delenv("GCONV_PATH", raising=False)
    assert ffmpeg_environment("/app/imageio_ffmpeg/binaries/ffmpeg")["GCONV_PATH"] == "/nonexistent"
    assert "GCONV_PATH" not in ffmpeg_environment("/usr/bin/ffmpeg")
    assert "GCONV_PATH" not in os.environ
    monkeypatch.setenv("GCONV_PATH", "/operator-config")
    assert ffmpeg_environment("/app/imageio_ffmpeg/binaries/ffmpeg")["GCONV_PATH"] == "/operator-config"


def test_active_reader_blocks_delete_and_late_reader_cannot_open_retired_file(database, tmp_path):
    path = tmp_path/"video.ts"
    path.write_bytes(b"media")
    with MediaReader(database, path):
        assert retire_file(database, path, tmp_path) is False
        assert path.read_bytes() == b"media"
    assert retire_file(database, path, tmp_path) is True
    with pytest.raises(MediaBusy):
        with MediaReader(database, path):
            pytest.fail("retired media admitted a new reader")


def test_expired_reader_does_not_prevent_retirement(database, tmp_path):
    path = tmp_path/"orphan.ts"
    path.write_bytes(b"media")
    database.media_readers.insert_one({"_id": key(path), "readers": [{"owner": "dead",
        "until": datetime.now(UTC)-timedelta(seconds=1)}]})
    assert retire_file(database, path, tmp_path)


def test_archive_retirement_keeps_all_parts_if_one_is_being_downloaded(database, tmp_path):
    paths = [tmp_path/"first.ts", tmp_path/"second.ts"]
    for path in paths:
        path.write_bytes(b"media")
    with MediaReader(database, paths[1]):
        assert retire_files(database, paths, tmp_path) is False
        assert all(p.exists() for p in paths)
    assert retire_files(database, paths, tmp_path)
    assert not any(p.exists() for p in paths)


def test_encoder_can_publish_while_asr_reads_original_then_defer_retirement(database, tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDINGS_ROOT", str(tmp_path))
    source, encoded = tmp_path/"video.ts", tmp_path/"video.mp4"
    source.write_bytes(b"source")
    encoded.write_bytes(b"encoded")
    r = row(database, file_path=str(encoded), original_file_path=str(source), encode_status="done",
            status="asr_processing", asr_state="waiting", asr_source_path=str(source))
    with MediaReader(database, source):
        reap_originals(database)
        assert source.exists()
        database.recordings.update_one({"_id": r["_id"]}, {"$set": {"asr_state": "done", "status": "asr_done"}})
        reap_originals(database)
        assert source.exists()
    reap_originals(database)
    assert not source.exists() and encoded.exists()
    assert database.recordings.find_one({"_id": r["_id"]})["asr_source_path"] == str(encoded)


@pytest.fixture
def video(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORDINGS_ROOT", str(tmp_path))
    path = tmp_path/"source.ts"
    subprocess.run([recorder._ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5", "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=48000",
        "-t", "8", "-c:v", "mpeg2video", "-c:a", "aac", "-muxrate", "500k", "-f", "mpegts", str(path)], check=True)
    return path


def test_ffmpeg_failure_keeps_first_attempt_before_pyav_fallback(database, video, tmp_path, monkeypatch):
    r = row(database, status="recording", asr_state="streaming")
    target = tmp_path/"recording.part.ts"

    def fail(url, output, duration):
        shutil.copy2(video, output)
        raise subprocess.CalledProcessError(1, ["ffmpeg"])

    monkeypatch.setattr(recorder, "_capture_with_ffmpeg", fail)
    monkeypatch.setattr(recorder, "_capture_with_pyav", lambda url, output, duration: shutil.copy2(video, output))
    method = recorder.capture_stream("http://example.invalid/live", target, datetime.now(UTC)+timedelta(seconds=30),
        on_partial=lambda path, method: save_partial(database, r, path, r["start_time"], method))
    saved = database.recordings.find_one({"_id": r["_id"]})
    assert method == "pyav" and target.exists()
    assert len(saved["recording_parts"]) == 1
    assert Path(saved["recording_parts"][0]["path"]).exists()


def test_terminal_capture_failure_preserves_playable_partial_for_ai(database, video, tmp_path):
    r = row(database, status="recording", scheduled_end=datetime.now(UTC)-timedelta(seconds=1))
    path = tmp_path/"capture.part.ts"
    shutil.copy2(video, path)
    save_partial(database, r, path, r["start_time"])
    recorder.handle_failure(database, r, RuntimeError("upstream interrupted"))
    saved = database.recordings.find_one({"_id": r["_id"]})
    assert saved["status"] == "completed" and saved["capture_state"] == "partial"
    assert saved["partial"] and Path(saved["file_path"]).exists()
    assert saved["asr_state"] == "waiting"


def test_failed_fallback_part_uses_its_own_start_time(database, video, tmp_path, monkeypatch):
    r = row(database, status="recording", source_url="http://example.invalid/live")
    database.channels.insert_one({"channel_id": r["channel_id"], "enabled": True})
    clock = [r["start_time"]]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    def interrupted(url, output, end, on_partial):
        shutil.copy2(video, output)
        clock[0] += timedelta(seconds=20)
        on_partial(output, "ffmpeg")
        shutil.copy2(video, output)
        raise RuntimeError("fallback also interrupted")

    monkeypatch.setattr(recorder, "datetime", Clock)
    monkeypatch.setattr(recorder, "RECORDING_ROOT", tmp_path)
    monkeypatch.setenv("LIVE_ASR_ENABLED", "0")
    monkeypatch.setattr(recorder, "capture_stream", interrupted)
    with pytest.raises(RuntimeError, match="fallback also interrupted"):
        recorder.process_recording(database, r)
    parts = database.recordings.find_one({"_id": r["_id"]})["recording_parts"]
    assert [p["offset"] for p in parts] == [0, 20]
    assert all(Path(p["path"]).exists() for p in parts)


def test_stale_worker_recovery_uses_an_immutable_snapshot(database, video):
    r = row(database, status="recording")
    before = video.stat().st_size
    part = save_partial(database, r, video, r["start_time"], "worker_lost")
    assert part and video.exists()
    with video.open("ab") as source:
        source.write(b"still being written")
    assert Path(part["path"]).stat().st_size == before
    assert save_partial(database, r, video, r["start_time"], "worker_lost") is None
    assert len(database.recordings.find_one({"_id": r["_id"]})["recording_parts"]) == 1


def test_coverage_reports_gaps_without_concatenating_sources(database):
    r = row(database)
    r["end_time"] = r["start_time"]+timedelta(seconds=40)
    r.update(partial=True, recording_parts=[{"id": "first", "offset": 0, "duration": 10, "path": "/private/a.ts"},
                                          {"id": "second", "offset": 20, "duration": 10, "path": "/private/b.ts"}])
    result = coverage(r)
    assert result["missing_intervals"] == [{"start": 10, "end": 20}, {"start": 30, "end": 40}]
    assert result["covered_seconds"] == 20 and all("path" not in p for p in result["parts"])


def test_source_recording_feedback_is_idempotent_and_preserves_lock(database):
    database.sources.insert_one({"source_id": "source", "url": "http://example.invalid/live", "locked": True, "priority": 1})
    r = row(database, source_id="source")
    report_source(database, r, False)
    report_source(database, r, False)
    report_source(database, {**r, "claim_id": "second"}, True)
    source = database.sources.find_one({"source_id": "source"})
    assert source["recording_health"]["score"] == 50 and source["recording_health"]["attempts"] == 2
    assert source["recording_failures_total"] == 1 and source["locked"] and source["priority"] == 1


def test_asr_reads_all_saved_attempts_on_their_own_timeline(database, tmp_path, monkeypatch):
    from iptv_pilot.asr import main as asr

    first, second = tmp_path/"first.ts", tmp_path/"second.ts"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    r = row(database, recording_parts=[{"id": "a", "path": str(first), "offset": 0},
                                      {"id": "b", "path": str(second), "offset": 30}])
    monkeypatch.setattr(asr, "_transcribe_file", lambda db, r: ([{"start": 0, "end": 2, "text": Path(r["file_path"]).stem}], ""))
    segments, text = asr.transcribe_recording(database, r)
    assert [s["start"] for s in segments] == [0, 30]
    assert [s["part_id"] for s in segments] == ["a", "b"]
    assert segments[1]["media_start"] == 0 and text == "first second"


def test_grounding_rejects_invented_quote_id_or_timestamp():
    class Provider(MockProvider):
        def summarize_grounded(self, payload):
            s = payload["segments"][0]
            return {**self.summarize(s["text"]), "evidence": [
                {"point_index": 0, "segment_id": "invented", "quote": s["text"]},
                {"point_index": 0, "segment_id": s["id"], "quote": "not in transcript"},
                {"point_index": 0, "segment_id": s["id"], "quote": "actual fact", "start": 9999}]}

    result = traceable.grounded(Provider(), [{"start": 10, "end": 20, "text": "An actual fact."}])
    assert len(result["evidence"]) == 1 and result["evidence"][0]["start"] == 10


def test_edit_only_recomputes_changed_note_and_never_pushes_by_default(database, monkeypatch):
    from iptv_pilot.llm import main as llm

    calls = []

    class Provider(MockProvider):
        def summarize_grounded(self, payload):
            calls.append(payload)
            return super().summarize_grounded(payload)

    r = row(database)
    segments = traceable.identify([{"start": 0, "end": 5, "text": "A number is ten."},
                                  {"start": 400, "end": 405, "text": "An unaffected fact."}])
    transcript = {"recording_id": r["_id"], "segments": segments, "full_text": "A number is ten. An unaffected fact."}
    database.transcripts.insert_one(transcript)
    traceable.final_summary(database, r, transcript, Provider())
    assert len(calls) == 3
    corrections.correct(database, r, segments[0]["id"], "A number is twelve.", 0)
    with pytest.raises(corrections.EditConflict):
        corrections.correct(database, r, segments[0]["id"], "stale edit", 0)
    monkeypatch.setattr(llm, "create_provider", Provider)
    llm.run_once(database)
    assert len(calls) == 5  # one changed note plus terminal merge
    saved = database.summaries.find_one({"recording_id": r["_id"]})
    assert saved["transcript_revision"] == 1 and saved["push_enabled"] is False
    assert saved["evidence"][0]["quote"] == "A number is twelve."
    edited = database.transcripts.find_one({"recording_id": r["_id"]})["segments"][0]
    assert edited["id"] == segments[0]["id"] and edited["original_text"] == "A number is ten."


def test_new_revision_delivery_does_not_reuse_old_generation_delivery(database, monkeypatch):
    from iptv_pilot.push import main as push

    r = row(database, transcript_revision=1)
    sid = database.summaries.insert_one({"recording_id": r["_id"], "generation": 2, "transcript_revision": 1,
        "push_enabled": True, "ready_for_push": True, "created_at": datetime.now(UTC)}).inserted_id
    database.push_events.insert_one({"summary_id": sid, "provider": "test", "status": "delivered", "generation": 1})
    delivered = []

    class Provider:
        name = "test"

        def push_summary(self, summary):
            delivered.append(summary["generation"])
            return True

    monkeypatch.setattr(push, "load_config", lambda db: {"channels": ["test"]})
    monkeypatch.setattr(push, "create_providers", lambda *args: [Provider()])
    push.run_once(database)
    push.run_once(database)
    assert delivered == [2]


def test_correction_rejects_a_running_capture(database):
    r = row(database, status="recording")
    segment = traceable.identify([{"start": 0, "end": 1, "text": "original"}])[0]
    database.transcripts.insert_one({"recording_id": r["_id"], "segments": [segment]})
    with pytest.raises(corrections.EditConflict):
        corrections.correct(database, r, segment["id"], "changed", 0)


def test_transcript_search_is_literal_and_uses_corrected_text():
    from iptv_pilot.common.transcript import search_segments

    rows = [{"text": "News [update]"}, {"text": "人工校正后的新闻"}]
    assert search_segments(rows, "[update]") == rows[:1]
    assert search_segments(rows, "NEWS") == rows[:1]
    assert search_segments(rows, "校正") == rows[1:]
    assert search_segments(rows, "missing") == []


def test_subtitles_use_local_part_time_and_keep_corrected_unicode_text():
    from iptv_pilot.common.transcript import subtitles

    segments = [{"part_id": "part", "start": 65.25, "end": 67,
                 "media_start": 5.25, "media_end": 7, "text": "校正后的字幕 <示例>"}]
    with pytest.raises(ValueError, match="select a recording part"):
        subtitles(segments)
    assert "00:00:05,250 --> 00:00:07,000" in subtitles(segments, "srt", "part")
    assert "校正后的字幕" in subtitles(segments, "srt", "part")
    vtt = subtitles(segments, "vtt", "part")
    assert vtt.startswith("WEBVTT\n\n") and "00:00:05.250" in vtt and "&lt;示例&gt;" in vtt


def test_topic_chapters_use_verified_evidence_timing():
    summary = {"key_points": ["新闻话题"], "evidence": [
        {"point_index": 0, "segment_id": "s", "part_id": "main", "start": 10,
         "end": 12, "media_start": 10, "media_end": 12}]}
    chapters = traceable.topic_chapters(summary, [])
    assert chapters[0]["title"] == "新闻话题" and chapters[0]["start"] == 10


def test_transcript_search_and_subtitle_api(database, monkeypatch):
    from fastapi.testclient import TestClient

    from iptv_pilot.web import main as web

    monkeypatch.setattr(web, "database", database)
    r = row(database)
    segments = traceable.identify([{"start": 5, "end": 7, "text": "新闻原文"}])
    database.transcripts.insert_one({"recording_id": r["_id"], "segments": segments, "revision": 2})
    client = TestClient(web.app)
    root = f"/api/recordings/{r['_id']}/transcript"
    page = client.get(root, params={"q": "原文"}).json()
    assert page["total"] == 1 and page["revision"] == 2
    assert client.get(root, params={"q": "没有"}).json()["total"] == 0
    response = client.get(root+"/subtitles?format=srt")
    assert response.status_code == 200 and "00:00:05,000" in response.text
    assert response.headers["content-disposition"].endswith('.srt"')
    assert client.get(root+"/subtitles?format=invalid").status_code == 422
