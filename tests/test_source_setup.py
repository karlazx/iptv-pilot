import os
import subprocess
import sys
from pathlib import Path

from scripts.import_live_sources import load_dotenv_value, resolve_compose_path


def test_load_dotenv_value_and_resolve_relative_path(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nIPTV_API_CONFIG_PATH='runtime/iptv-api-config'\n",
        encoding="utf-8",
    )
    value = load_dotenv_value(env_file, "IPTV_API_CONFIG_PATH")
    assert value == "runtime/iptv-api-config"
    assert resolve_compose_path(value, tmp_path) == tmp_path / "runtime/iptv-api-config"


def test_configure_iptv_api_without_env_file_is_idempotent(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.ini").write_text("[Settings]\n", encoding="utf-8")
    (config_dir / "demo.txt").write_text("默认分组,#genre#\n", encoding="utf-8")
    (config_dir / "subscribe.txt").write_text("[WHITELIST]\n", encoding="utf-8")
    channels_file = tmp_path / "channels.txt"
    channels_file.write_text(
        "我的频道,https://stream.example.com/live/index.m3u8\n",
        encoding="utf-8",
    )
    subscriptions_file = tmp_path / "subscriptions.txt"
    subscriptions_file.write_text(
        "https://provider.example.com/authorized.m3u\n",
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "scripts" / "configure_iptv_api.py"
    command = [
        sys.executable,
        str(script),
        str(config_dir),
        "--channels-file",
        str(channels_file),
        "--subscriptions-file",
        str(subscriptions_file),
        "--owner",
        f"{os.getuid()}:{os.getgid()}",
    ]

    subprocess.run(command, check=True)
    subprocess.run(command, check=True)

    assert (config_dir / "local" / "iptv_pilot_channels.txt").read_text(
        encoding="utf-8"
    ) == channels_file.read_text(encoding="utf-8")
    demo = (config_dir / "demo.txt").read_text(encoding="utf-8")
    subscriptions = (config_dir / "subscribe.txt").read_text(encoding="utf-8")
    assert demo.count("# BEGIN iptv-pilot user channels") == 1
    assert "我的频道" in demo
    assert subscriptions.count("# BEGIN iptv-pilot user subscriptions") == 1
    assert subscriptions.index("authorized.m3u") < subscriptions.index("[WHITELIST]")
    assert (config_dir / "subscribe.txt").stat().st_uid == os.getuid()
