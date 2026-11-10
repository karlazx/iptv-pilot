import os
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

import pytest
from pydantic import ValidationError

from iptv_pilot import __version__
from iptv_pilot.epg_engine.main import iter_xmltv, parse_xmltv_time
from iptv_pilot.live_normalizer.main import (
    ffmpeg_command,
    manifest_entries,
    segment_timing,
    validate_segment_timeline,
)
from iptv_pilot.llm.providers import MockProvider, validate_summary
from iptv_pilot.recorder.main import (
    _capture_with_ffmpeg,
    _validate_captured_duration,
    capture_stream,
    handle_failure,
    max_per_channel,
    recording_concurrency,
    safe_name,
)
from iptv_pilot.scheduler.main import matches, schedule_once
from iptv_pilot.web.live import LiveProxyError, LiveTokenSigner, rewrite_hls_manifest
from iptv_pilot.web.main import SubscriptionCreate, TimeRule
from scripts.channels.refresh_stream_quality import preferred_index, should_switch


def test_package_version_matches_metadata():
    assert __version__ == version("iptv-pilot")


def test_parse_xmltv_time_with_offset():
    parsed = parse_xmltv_time("20260812190000 +0800")
    assert parsed.tzinfo == UTC
    assert parsed.hour == 11


def test_iter_xmltv():
    payload = b"""<tv><channel id="c1"><display-name>CCTV-1</display-name></channel>
    <programme channel="c1" start="20260812190000 +0800" stop="20260812193000 +0800">
    <title>News</title><desc>Daily</desc></programme></tv>"""
    rows = list(iter_xmltv(payload))
    assert rows[0][1]["channel_id"] == "c1"
    assert rows[1][1]["title"] == "News"


def test_mock_summary_shape():
    summary = MockProvider().summarize("hello")
    validate_summary(summary)
    assert summary["key_points"]


def test_safe_name():
    assert safe_name("新闻/联播:晚间") == "新闻_联播_晚间"


def test_recording_duration_rejects_early_eof(monkeypatch, tmp_path):
    monkeypatch.setattr("iptv_pilot.recorder.main.probe_media_duration", lambda _path: 10.1)
    with pytest.raises(RuntimeError, match="ended early"):
        _validate_captured_duration(tmp_path / "x.ts", 2036)


def test_recording_duration_accepts_complete_capture(monkeypatch, tmp_path):
    monkeypatch.setattr("iptv_pilot.recorder.main.probe_media_duration", lambda _path: 58.0)
    assert _validate_captured_duration(tmp_path / "x.ts", 60) == 58.0


def test_recording_duration_skips_short_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("iptv_pilot.recorder.main.probe_media_duration", lambda _path: 10.0)
    assert _validate_captured_duration(tmp_path / "x.ts", 30) == 10.0


def test_recording_duration_skips_unprobeable(monkeypatch, tmp_path):
    monkeypatch.setattr("iptv_pilot.recorder.main.probe_media_duration", lambda _path: None)
    assert _validate_captured_duration(tmp_path / "x.ts", 600) is None


def test_recording_concurrency_is_clamped(monkeypatch):
    monkeypatch.setenv("RECORDING_CONCURRENCY", "99")
    assert recording_concurrency() == 8
    monkeypatch.setenv("RECORDING_CONCURRENCY", "0")
    assert recording_concurrency() == 1
    monkeypatch.setenv("RECORDING_CONCURRENCY", "not-a-number")
    assert recording_concurrency() == 2


def test_per_channel_limit_never_exceeds_concurrency(monkeypatch):
    monkeypatch.setenv("RECORDING_MAX_PER_CHANNEL", "8")
    assert max_per_channel(3) == 3
    monkeypatch.setenv("RECORDING_MAX_PER_CHANNEL", "0")
    assert max_per_channel(3) == 1


def test_recording_failure_is_fenced_by_claim_id(monkeypatch):
    class Result:
        matched_count = 0

    class Recordings:
        def __init__(self):
            self.filters = []

        def update_one(self, filter_, _update):
            self.filters.append(filter_)
            return Result()

    class Database:
        def __init__(self):
            self.recordings = Recordings()

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    database = Database()
    recording = {
        "_id": "recording-1",
        "claim_id": "stale-claim",
        "retry_count": 0,
        "scheduled_end": datetime.now(UTC) + timedelta(minutes=10),
    }
    handle_failure(database, recording, RuntimeError("boom"))
    assert database.recordings.filters == [{"_id": "recording-1", "claim_id": "stale-claim"}]



def test_ffmpeg_capture_uses_read_timeout_without_unbounded_reconnect(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command

    monkeypatch.setattr("iptv_pilot.recorder.main._ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr("iptv_pilot.recorder.main.subprocess.run", fake_run)
    _capture_with_ffmpeg("http://example.test/live.m3u8", tmp_path / "out.ts", 10)
    command = captured["command"]
    assert "-rw_timeout" in command
    assert "15000000" in command
    assert "-reconnect" not in command


def test_recorder_falls_back_to_pyav(monkeypatch, tmp_path):
    calls = []

    def fail_ffmpeg(*_args):
        calls.append("ffmpeg")
        raise RuntimeError("segfault")

    def pass_pyav(_url, path, _duration):
        calls.append("pyav")
        path.write_bytes(b"transport-stream")

    monkeypatch.setattr("iptv_pilot.recorder.main._capture_with_ffmpeg", fail_ffmpeg)
    monkeypatch.setattr("iptv_pilot.recorder.main._capture_with_pyav", pass_pyav)
    target = tmp_path / "capture.ts"
    method = capture_stream("https://example.test/live.m3u8", target, datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=30))
    assert method == "pyav"
    assert calls == ["ffmpeg", "pyav"]
    assert target.read_bytes() == b"transport-stream"


def test_live_manifest_rewrite_and_signed_url():
    signer = LiveTokenSigner(secret=b"test-secret", lifetime_seconds=60)
    manifest = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\nsegments/001.ts\n'
    rewritten = rewrite_hls_manifest(manifest, "https://media.example/live/index.m3u8", signer)
    proxy_urls = [part for part in rewritten.replace('"', "\n").splitlines() if part.startswith("/api/live/proxy/")]
    assert len(proxy_urls) == 2
    decoded = {signer.verify(value.rsplit("/", 1)[-1]) for value in proxy_urls}
    assert decoded == {
        "https://media.example/live/key.bin",
        "https://media.example/live/segments/001.ts",
    }


def test_live_token_rejects_tampering():
    signer = LiveTokenSigner(secret=b"test-secret")
    token = signer.sign("https://media.example/live.m3u8")
    payload, signature = token.split(".", 1)
    signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(LiveProxyError):
        signer.verify(f"{payload}.{signature}")


def test_live_token_is_stable_inside_time_bucket(monkeypatch):
    signer = LiveTokenSigner(secret=b"test-secret", lifetime_seconds=60)
    monkeypatch.setattr("iptv_pilot.web.live.time.time", lambda: 120.1)
    first = signer.sign("https://media.example/001.ts")
    monkeypatch.setattr("iptv_pilot.web.live.time.time", lambda: 179.9)
    assert signer.sign("https://media.example/001.ts") == first
    monkeypatch.setattr("iptv_pilot.web.live.time.time", lambda: 180.1)
    assert signer.sign("https://media.example/001.ts") != first


def test_keyword_match_in_description_and_optional_channel():
    programme = {"channel_id": "CCTV-1", "title": "晚间节目", "description": "含有经济新闻"}
    assert matches({"match_type": "keyword", "keyword": "经济", "channel_id": "CCTV-1"}, programme)
    assert not matches({"match_type": "keyword", "keyword": "经济", "channel_id": "CCTV-2"}, programme)


def test_channel_time_uses_local_timezone_and_tolerance():
    programme = {
        "channel_id": "CCTV-1",
        "start_time": datetime(2026, 8, 16, 16, 2, tzinfo=UTC),
    }
    subscription = {
        "match_type": "channel_time",
        "channel_id": "CCTV-1",
        "time_rule": {"weekdays": [0], "time": "00:00", "tolerance_min": 3},
    }
    assert matches(subscription, programme, "Asia/Shanghai")


def test_subscription_models_validate_rules():
    rule = TimeRule(weekdays=[0, 4], time="20:00", timezone="Asia/Shanghai")
    assert rule.time == "20:00"
    with pytest.raises(ValidationError):
        SubscriptionCreate(name="bad", match_type="keyword")


def test_live_normalizer_keeps_sixty_second_window(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVE_NORMALIZE_HLS_LIST_SIZE", raising=False)
    command = ffmpeg_command("https://example.test/live.m3u8", tmp_path)
    assert command[command.index("-hls_list_size") + 1] == "30"
    assert command[command.index("-af") + 1] == "aresample=async=1:first_pts=0"
    assert int(command[command.index("-start_number") + 1]) > 0


def test_segment_timeline_accepts_aligned_tracks(monkeypatch, tmp_path):
    class Result:
        stdout = '{"streams":[{"codec_type":"video","start_time":"10.0","duration":"2.0"},{"codec_type":"audio","start_time":"10.2","duration":"1.9"}]}'

    monkeypatch.setattr("iptv_pilot.live_normalizer.main.subprocess.run", lambda *_a, **_kw: Result())
    assert validate_segment_timeline(tmp_path / "segment.ts") == pytest.approx(0.2)


def test_segment_timeline_rejects_large_drift(monkeypatch, tmp_path):
    class Result:
        stdout = '{"streams":[{"codec_type":"video","start_time":"10.0","duration":"2.0"},{"codec_type":"audio","start_time":"97121.0","duration":"1.9"}]}'

    monkeypatch.setattr("iptv_pilot.live_normalizer.main.subprocess.run", lambda *_a, **_kw: Result())
    with pytest.raises(RuntimeError, match="timeline drift"):
        validate_segment_timeline(tmp_path / "segment.ts")


def test_segment_timeline_rejects_long_audio_track(monkeypatch, tmp_path):
    class Result:
        stdout = '{"streams":[{"codec_type":"video","start_time":"10.0","duration":"2.0"},{"codec_type":"audio","start_time":"10.1","duration":"12.0"}]}'

    monkeypatch.setattr("iptv_pilot.live_normalizer.main.subprocess.run", lambda *_a, **_kw: Result())
    with pytest.raises(RuntimeError, match="audio duration"):
        validate_segment_timeline(tmp_path / "segment.ts")


def test_manifest_entries_preserve_segment_tags(tmp_path):
    manifest = tmp_path / "index.m3u8"
    manifest.write_text(
        "#EXTM3U\n#EXTINF:2.000000,\n#EXT-X-PROGRAM-DATE-TIME:2026-08-14T22:00:00+0800\nsegment_000000001.ts\n",
        encoding="utf-8",
    )
    assert manifest_entries(manifest) == [
        (
            "segment_000000001.ts",
            (
                "#EXTINF:2.000000,",
                "#EXT-X-PROGRAM-DATE-TIME:2026-08-14T22:00:00+0800",
            ),
        )
    ]


def test_segment_timing_detects_stalls_and_bursts(tmp_path):
    mtimes = [100.0, 102.0, 106.5, 106.7]
    for index, mtime in enumerate(mtimes):
        path = tmp_path / f"segment_{index:09d}.ts"
        path.write_bytes(b"segment")
        path.touch()
        os.utime(path, (mtime, mtime))
    result = segment_timing(tmp_path)
    assert result["stall_count"] == 1
    assert result["burst_count"] == 1


def test_stream_quality_prefers_runtime_clean_candidate():
    results = [
        {"valid": True, "sustainable": True, "download_ratio": 0.2, "required_mbps": 8},
        {
            "valid": True,
            "sustainable": True,
            "download_ratio": 0.7,
            "required_mbps": 4,
            "media_healthy": True,
        },
    ]
    assert preferred_index(results) == 1
    assert not should_switch(results[1], results[0])


def test_scheduler_dedupes_same_channel_and_start(monkeypatch):
    start = datetime.now(UTC) + timedelta(minutes=5)

    class Cursor(list):
        pass

    class Subscriptions:
        def find(self, _filter):
            return Cursor(
                [
                    {"_id": "sub-1", "match_type": "keyword", "keyword": "新闻", "enabled": True},
                    {"_id": "sub-2", "match_type": "keyword", "keyword": "新闻", "enabled": True},
                ]
            )

    class Programmes:
        def find(self, _filter):
            return Cursor(
                [
                    {
                        "channel_id": "CCTV-1",
                        "title": "新闻联播",
                        "description": "",
                        "start_time": start,
                        "end_time": start + timedelta(minutes=30),
                    }
                ]
            )

    class Channels:
        def distinct(self, _field, _filter):
            return ["CCTV-1"]

    class Recordings:
        def __init__(self):
            self.operations = []

        def bulk_write(self, operations, ordered=False):
            self.operations = operations
            assert ordered is False

            class Result:
                upserted_count = 1

            return Result()

    class Database:
        def __init__(self):
            self.subscriptions = Subscriptions()
            self.epg_programs = Programmes()
            self.channels = Channels()
            self.recordings = Recordings()

    database = Database()
    schedule_once(database)
    assert len(database.recordings.operations) == 1
    operation = database.recordings.operations[0]
    assert operation._filter == {"channel_id": "CCTV-1", "start_time": start}
    assert operation._doc["$addToSet"]["subscription_ids"]["$each"] == ["sub-1", "sub-2"]


class _FailureDB:
    """Minimal stand-in that records the update the recorder would apply."""

    def __init__(self):
        self.filter = None
        self.update = None

    class _Recordings:
        def __init__(self, owner):
            self.owner = owner

        def update_one(self, filter_, update):
            self.owner.filter = filter_
            self.owner.update = update

            class Result:
                matched_count = 1

            return Result()

    @property
    def recordings(self):
        return self._Recordings(self)


def _failure_recording(**overrides):
    base = {
        "_id": "rec-1",
        "claim_id": "claim-1",
        "channel_id": "CCTV-1",
        "retry_count": 0,
        "scheduled_start": datetime.now(UTC) - timedelta(minutes=5),
        "scheduled_end": datetime.now(UTC) + timedelta(minutes=30),
        "window_seconds": 2160,
    }
    base.update(overrides)
    return base


def test_retry_is_skipped_when_little_window_remains(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    monkeypatch.setenv("RECORDING_RETRY_MIN_FRACTION", "0.2")
    monkeypatch.setenv("RECORDING_MIN_WINDOW_SECONDS", "60")
    database = _FailureDB()
    # 36-minute window with 4 minutes left: only the post-roll is ahead, so a
    # retry would capture a few seconds of tail and fail again.
    recording = _failure_recording(scheduled_end=datetime.now(UTC) + timedelta(minutes=4))
    handle_failure(database, recording, RuntimeError("recording ended early"))
    assert database.update["$set"]["status"] == "failed"
    assert database.update["$set"]["skip_reason"] == "insufficient_window"


def test_retry_is_allowed_when_most_of_the_window_remains(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    monkeypatch.setenv("RECORDING_RETRY_MIN_FRACTION", "0.2")
    database = _FailureDB()
    recording = _failure_recording(scheduled_end=datetime.now(UTC) + timedelta(minutes=30))
    handle_failure(database, recording, RuntimeError("recording ended early"))
    assert database.update["$set"]["status"] == "queued"
    assert database.update["$set"]["next_attempt_at"] > datetime.now(UTC)


def test_upstream_errors_back_off_longer_than_stream_errors(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    monkeypatch.setenv("RECORDING_RETRY_DELAY_SECONDS", "30")
    monkeypatch.setenv("RECORDING_RETRY_UPSTREAM_DELAY_SECONDS", "120")

    stream_db = _FailureDB()
    handle_failure(stream_db, _failure_recording(), RuntimeError("captured short"))
    stream_delay = stream_db.update["$set"]["next_attempt_at"] - datetime.now(UTC)

    upstream_db = _FailureDB()
    handle_failure(upstream_db, _failure_recording(), RuntimeError("HTTP error 502 Bad Gateway"))
    upstream_delay = upstream_db.update["$set"]["next_attempt_at"] - datetime.now(UTC)

    assert upstream_delay > stream_delay
    assert upstream_delay >= timedelta(seconds=110)


def test_retry_stops_immediately_when_the_window_is_closed(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    database = _FailureDB()
    recording = _failure_recording(scheduled_end=datetime.now(UTC) - timedelta(minutes=1))
    handle_failure(database, recording, RuntimeError("boom"))
    assert database.update["$set"]["skip_reason"] == "window_expired"


class _SwitchDB:
    """Failure sink plus the channel/source lookup the recorder needs."""

    def __init__(self, sources=None):
        self.filter = None
        self.update = None
        self.rows = sources or []
        owner = self

        class Channels:
            def find_one(self, *_args, **_kwargs):
                return {"channel_id": "CCTV-1", "source_policy": "locked_primary"}

        class Sources:
            def find(self, *_args, **_kwargs):
                return list(owner.rows)

        class Recordings:
            def update_one(self, filter_, update):
                owner.filter = filter_
                owner.update = update

                class Result:
                    matched_count = 1

                return Result()

        self.channels = Channels()
        self.sources = Sources()
        self.recordings = Recordings()


def _source_row(source_id, url, priority, enabled=True):
    return {
        "source_id": source_id,
        "url": url,
        "priority": priority,
        "enabled": enabled,
        "locked": priority == 0,
    }


def test_retry_keeps_the_same_source_first(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    monkeypatch.setenv("RECORDING_SOURCE_RETRIES", "1")
    database = _SwitchDB(
        sources=[
            _source_row("s-primary", "http://a", 0),
            _source_row("s-backup", "http://b", 1),
        ]
    )
    recording = _failure_recording(
        source_id="s-primary",
        source_url="http://a",
        source_attempts=0,
        tried_source_ids=["s-primary"],
    )
    handle_failure(database, recording, RuntimeError("captured short"))
    # First failure must stay on the pinned URL.
    assert database.update["$set"]["status"] == "queued"
    assert "source_url" not in database.update["$set"]


def test_second_failure_switches_to_an_untried_source(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    monkeypatch.setenv("RECORDING_SOURCE_RETRIES", "1")
    monkeypatch.setenv("RECORDING_MAX_SOURCE_SWITCHES", "2")
    database = _SwitchDB(
        sources=[
            _source_row("s-primary", "http://a", 0),
            _source_row("s-backup", "http://b", 1),
        ]
    )
    recording = _failure_recording(
        source_id="s-primary",
        source_url="http://a",
        source_attempts=1,
        tried_source_ids=["s-primary"],
    )
    handle_failure(database, recording, RuntimeError("captured short"))
    update = database.update["$set"]
    assert update["status"] == "queued"
    assert update["source_id"] == "s-backup"
    assert update["source_url"] == "http://b"
    assert update["source_switches"] == 1
    assert update["source_attempts"] == 0
    assert update["retry_count"] == 0


def test_switch_is_skipped_when_no_source_is_left(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    monkeypatch.setenv("RECORDING_SOURCE_RETRIES", "0")
    monkeypatch.setenv("RECORDING_MAX_SOURCE_SWITCHES", "2")
    database = _SwitchDB(sources=[_source_row("s-primary", "http://a", 0)])
    recording = _failure_recording(
        source_id="s-primary",
        source_url="http://a",
        source_attempts=1,
        tried_source_ids=["s-primary"],
    )
    handle_failure(database, recording, RuntimeError("captured short"))
    assert database.update["$set"]["status"] == "failed"
    assert database.update["$set"]["skip_reason"] == "sources_exhausted"


def test_source_switch_count_is_capped(monkeypatch):
    from iptv_pilot.recorder.main import handle_failure

    monkeypatch.setenv("RECORDING_MAX_RETRIES", "2")
    monkeypatch.setenv("RECORDING_SOURCE_RETRIES", "0")
    monkeypatch.setenv("RECORDING_MAX_SOURCE_SWITCHES", "1")
    database = _SwitchDB(
        sources=[
            _source_row("s-primary", "http://a", 0),
            _source_row("s-backup", "http://b", 1),
        ]
    )
    recording = _failure_recording(
        source_id="s-primary",
        source_url="http://a",
        source_attempts=0,
        source_switches=1,
        tried_source_ids=["s-primary"],
    )
    handle_failure(database, recording, RuntimeError("captured short"))
    # Per-source budget is spent and the rotation budget is spent too, so the
    # task fails instead of looping over the same pair of sources.
    assert database.update["$set"]["status"] == "failed"
    assert database.update["$set"]["skip_reason"] == "retry_exhausted"
    assert "source_url" not in database.update["$set"]
