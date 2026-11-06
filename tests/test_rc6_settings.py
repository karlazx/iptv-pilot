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
