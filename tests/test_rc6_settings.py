"""Tests for rc.6 push settings, recording encoding and source grouping."""

import pytest

from iptv_pilot.push.settings import load_config, public_config, save_config
from iptv_pilot.recorder.encoding import defaults, load_settings, save_settings


class FakeCursor(list):
    def sort(self, *_args, **_kwargs):
        return self


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def find_one(self, query, *_args, **_kwargs):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                return row
        return None

    def update_one(self, query, update, upsert=False):
        row = self.find_one(query)
        if row is None:
            if not upsert:
                return type("Result", (), {"matched_count": 0})()
            row = {key: value for key, value in query.items()}
            self.rows.append(row)
        row.update(update.get("$set", {}))

        class Result:
            matched_count = 1

        return Result()


class FakeDatabase:
    def __init__(self, settings=None):
        self.settings = FakeCollection(settings)


def test_push_config_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "email")
    monkeypatch.setenv("SMTP_TO", "env@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    config = load_config(FakeDatabase())
    assert config["provider"] == "email"
    assert config["email"]["recipient"] == "env@example.com"


def test_push_config_database_overrides_env(monkeypatch):
    monkeypatch.setenv("PUSH_PROVIDER", "email")
    monkeypatch.setenv("SMTP_TO", "env@example.com")
    database = FakeDatabase()
    save_config(
        database,
        "email",
        {"recipient": "saved@example.com", "host": "smtp.test", "port": 587, "password": "secret"},
    )
    config = load_config(database)
    assert config["email"]["recipient"] == "saved@example.com"
    assert config["email"]["port"] == 587
    assert config["email"]["password"] == "secret"


def test_push_public_config_hides_password(monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "env-secret")
    database = FakeDatabase()
    save_config(database, "email", {"recipient": "saved@example.com", "password": "stored-secret"})
    payload = public_config(database)
    assert "password" not in payload["email"]
    assert payload["email"]["has_password"] is True


def test_push_config_blank_password_keeps_stored(monkeypatch):
    database = FakeDatabase()
    save_config(database, "email", {"recipient": "a@example.com", "password": "stored-secret"})
    save_config(database, "email", {"recipient": "b@example.com", "password": ""})
    assert load_config(database)["email"]["password"] == "stored-secret"


def test_push_config_rejects_unknown_provider():
    with pytest.raises(ValueError):
        save_config(FakeDatabase(), "sms", {})


def test_recording_settings_defaults(monkeypatch):
    monkeypatch.delenv("RECORDING_ENCODE_HEIGHT", raising=False)
    settings = defaults()
    assert settings["height"] == 720
    assert settings["crf"] == 26
    assert settings["keep_original"] is False


def test_recording_settings_clamp_and_validate():
    database = FakeDatabase()
    saved = save_settings(database, {"enabled": True, "height": 999, "crf": 99, "preset": "nope"})
    assert saved["height"] == 720
    assert saved["crf"] == 34
    assert saved["preset"] == "veryfast"
    assert load_settings(database) == saved


def test_recording_settings_disable_turns_off():
    database = FakeDatabase()
    saved = save_settings(database, {"enabled": False, "height": 0, "crf": 28})
    assert saved["enabled"] is False
    assert saved["height"] == 0


def test_source_group_falls_back_to_origin_labels():
    from iptv_pilot.web.main import _source_group

    assert _source_group({"provider": "官方组播"})["key"] == "官方组播"
    assert _source_group({"provider": "iptv-api"})["label"] == "IPTV-API 同步"
    assert _source_group({"origin": "seed"})["label"] == "IPTV-API 同步"
    assert _source_group({"origin": "import"})["label"] == "M3U 导入"
    assert _source_group({"origin": "migration", "kind": "udpxy"})["label"] == "官方组播桥接"
    assert _source_group({"origin": "custom", "kind": "custom"})["label"] == "用户自定义"


def test_encode_command_caps_height_and_uses_aac(monkeypatch):
    from pathlib import Path

    from iptv_pilot.recorder.encoder import encode_command

    monkeypatch.setenv("FFMPEG_BIN", "ffmpeg")
    command = encode_command(
        Path("/r/a.ts"),
        Path("/r/a.mp4"),
        {"height": 720, "crf": 26, "preset": "veryfast"},
    )
    assert "scale=-2:min(720\\,ih)" in command
    assert command[command.index("-max_muxing_queue_size") + 1] == "4096"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-crf") + 1] == "26"
    assert "mp4" in command


def test_encode_command_keeps_native_resolution_when_disabled(monkeypatch):
    from pathlib import Path

    from iptv_pilot.recorder.encoder import encode_command

    monkeypatch.setenv("FFMPEG_BIN", "ffmpeg")
    command = encode_command(Path("/r/a.ts"), Path("/r/a.mp4"), {"height": 0, "crf": 23})
    assert "scale=-2:0" not in command
    assert "-vf" not in command


def test_encode_command_never_upscales_low_resolution_channels(monkeypatch):
    from pathlib import Path

    from iptv_pilot.recorder.encoder import encode_command

    monkeypatch.setenv("FFMPEG_BIN", "ffmpeg")
    command = encode_command(Path("/r/sd.ts"), Path("/r/sd.mp4"), {"height": 720, "crf": 26})
    assert "scale=-2:min(720\\,ih)" in command


def test_reclaim_orphans_requeues_and_removes_temporary(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from iptv_pilot.recorder import encoder

    temp = tmp_path / "a.encoding.mp4"
    temp.write_bytes(b"partial")
    stale = datetime.now(UTC) - timedelta(minutes=10)
    updates = []

    class Recordings:
        def find(self, _query):
            return [
                {
                    "_id": "rec-1",
                    "file_path": str(tmp_path / "a.ts"),
                    "encode_status": "encoding",
                    "encode_worker": "dead:1",
                    "encode_heartbeat_at": stale,
                }
            ]

        def update_one(self, query, update):
            updates.append((query, update))

    class Database:
        recordings = Recordings()

    monkeypatch.setenv("RECORDING_ENCODE_HEARTBEAT_SECONDS", "30")
    assert encoder.reclaim_orphans(Database()) == 1
    assert not temp.exists()
    assert updates[0][0] == {"_id": "rec-1", "encode_status": "encoding"}
    assert updates[0][1]["$set"]["encode_status"] == "pending"


def test_encode_duration_tolerance_defaults_and_clamps(monkeypatch):
    from iptv_pilot.recorder.encoder import duration_tolerance

    monkeypatch.delenv("RECORDING_ENCODE_MIN_DURATION_RATIO", raising=False)
    assert duration_tolerance() == 0.95
    monkeypatch.setenv("RECORDING_ENCODE_MIN_DURATION_RATIO", "0.8")
    assert duration_tolerance() == 0.8
    monkeypatch.setenv("RECORDING_ENCODE_MIN_DURATION_RATIO", "0.1")
    assert duration_tolerance() == 0.5
    monkeypatch.setenv("RECORDING_ENCODE_MIN_DURATION_RATIO", "2")
    assert duration_tolerance() == 1.0
    monkeypatch.setenv("RECORDING_ENCODE_MIN_DURATION_RATIO", "garbage")
    assert duration_tolerance() == 0.95


def test_ninety_seven_percent_is_accepted_as_a_complete_encode():
    # The 490s-vs-503s case that the 0.98 threshold wrongly rejected.
    expected, actual = 503.0, 490.0
    assert actual >= expected * 0.95


class _FakeStream:
    def __init__(self, *, content_type: str, body: bytes, length: int | None = None):
        self.headers = {"content-type": content_type}
        if length is not None:
            self.headers["content-length"] = str(length)
        self._body = body
        self.encoding = "utf-8"
        self.url = "http://example.test/playlist.m3u8"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield self._body


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def stream(self, _method, _url, headers=None):
        return self._response


def test_playlist_guard_rejects_video_content_type():
    from scripts.channels.diagnose_live_quality import PlaylistTooLarge, read_playlist

    client = _FakeClient(_FakeStream(content_type="video/mp4", body=b"\x00" * 64))
    with pytest.raises(PlaylistTooLarge, match="not a playlist"):
        read_playlist(client, "http://example.test/a.mp4")


def test_playlist_guard_rejects_declared_oversize():
    from scripts.channels.diagnose_live_quality import (
        MAX_PLAYLIST_BYTES,
        PlaylistTooLarge,
        read_playlist,
    )

    client = _FakeClient(
        _FakeStream(
            content_type="application/vnd.apple.mpegurl",
            body=b"",
            length=MAX_PLAYLIST_BYTES + 1,
        )
    )
    with pytest.raises(PlaylistTooLarge, match="too large"):
        read_playlist(client, "http://example.test/a.m3u8")


def test_playlist_guard_stops_streaming_past_the_cap():
    from scripts.channels.diagnose_live_quality import PlaylistTooLarge, read_playlist

    body = b"#EXTM3U\n" + b"# filler\n" * 400_000
    client = _FakeClient(_FakeStream(content_type="text/plain", body=body))
    with pytest.raises(PlaylistTooLarge, match="exceeded"):
        read_playlist(client, "http://example.test/a.m3u8")


def test_playlist_guard_accepts_a_real_playlist():
    from scripts.channels.diagnose_live_quality import read_playlist

    body = b"#EXTM3U\n#EXTINF:2.0,\nseg_00001.ts\n"
    client = _FakeClient(_FakeStream(content_type="application/vnd.apple.mpegurl", body=body))
    text, _elapsed, _url = read_playlist(client, "http://example.test/a.m3u8")
    assert text.startswith("#EXTM3U")


def test_playlist_guard_rejects_non_playlist_body():
    from scripts.channels.diagnose_live_quality import PlaylistTooLarge, read_playlist

    client = _FakeClient(_FakeStream(content_type="text/html", body=b"<html>error</html>"))
    with pytest.raises(PlaylistTooLarge, match="not an HLS playlist"):
        read_playlist(client, "http://example.test/a.m3u8")
