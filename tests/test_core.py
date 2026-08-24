import os
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from iptv_pilot.epg_engine.main import iter_xmltv, parse_xmltv_time
from iptv_pilot.live_normalizer.main import (
    ffmpeg_command,
    manifest_entries,
    segment_timing,
    validate_segment_timeline,
)
from iptv_pilot.llm.providers import MockProvider, validate_summary
from iptv_pilot.recorder.main import capture_stream, safe_name
from iptv_pilot.scheduler.main import matches
from iptv_pilot.web.live import LiveProxyError, LiveTokenSigner, rewrite_hls_manifest
from iptv_pilot.web.main import SubscriptionCreate, TimeRule
from scripts.refresh_stream_quality import preferred_index, should_switch


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
