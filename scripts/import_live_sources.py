#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def load_dotenv_value(path: Path, key: str) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    raise SystemExit(f"{path} 中未配置 {key}")


def resolve_compose_path(value: str, project_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (project_dir / path).resolve()


def existing_file(value: Path | None, label: str) -> Path | None:
    if value is None:
        return None
    path = value.expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"{label}不存在：{path}")
    return path


def run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入用户自有或已获授权的 IPTV 直播源")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--channels-file", type=Path, help="频道文件，格式为“频道名称,直播 URL”")
    parser.add_argument("--subscriptions-file", type=Path, help="订阅文件，每行一个 M3U/TXT URL")
    parser.add_argument("--no-restart", action="store_true", help="写入配置后不重启相关服务")
    args = parser.parse_args()

    if not args.channels_file and not args.subscriptions_file:
        parser.error("至少提供 --channels-file 或 --subscriptions-file")
    if not shutil.which("docker"):
        raise SystemExit("未找到 docker 命令")

    project_dir = Path(__file__).resolve().parents[1]
    env_file = args.env_file.expanduser()
    if not env_file.is_absolute():
        env_file = (project_dir / env_file).resolve()
    if not env_file.is_file():
        raise SystemExit(f"环境文件不存在：{env_file}")

    config_dir = resolve_compose_path(
        load_dotenv_value(env_file, "IPTV_API_CONFIG_PATH"), project_dir
    )
    if not (config_dir / "config.ini").is_file() or not (config_dir / "demo.txt").is_file():
        raise SystemExit(
            "iptv-api 配置尚未初始化；请先运行 docker compose up -d，"
            "等待 iptv-api 启动后重试"
        )

    channels_file = existing_file(args.channels_file, "频道文件")
    subscriptions_file = existing_file(args.subscriptions_file, "订阅文件")
    configure_script = project_dir / "scripts" / "configure_iptv_api.py"
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(project_dir),
        "--env-file",
        str(env_file),
    ]
    command = [
        *compose,
        "run",
        "--rm",
        "--no-deps",
        "-v",
        f"{config_dir}:/iptv-api/config",
        "-v",
        f"{configure_script}:/input/configure_iptv_api.py:ro",
    ]
    configure_args = [
        "web",
        "python",
        "/input/configure_iptv_api.py",
        "/iptv-api/config",
    ]
    if channels_file:
        command.extend(["-v", f"{channels_file}:/input/channels.txt:ro"])
        configure_args.extend(["--channels-file", "/input/channels.txt"])
    if subscriptions_file:
        command.extend(["-v", f"{subscriptions_file}:/input/subscriptions.txt:ro"])
        configure_args.extend(["--subscriptions-file", "/input/subscriptions.txt"])
    configure_args.extend(["--owner", f"{os.getuid()}:{os.getgid()}"])

    print("正在通过临时容器写入 iptv-api 持久化配置……")
    run([*command, *configure_args], project_dir)
    if not args.no_restart:
        print("正在重启 iptv-api 和频道同步服务……")
        run([*compose, "restart", "iptv-api", "channel-sync"], project_dir)
    print("直播源配置已写入；批量生成和频道同步通常需要几分钟。")


if __name__ == "__main__":
    main()
