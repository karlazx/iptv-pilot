#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

GROUP_BEGIN_MARKER = "# BEGIN iptv-pilot user channels"
GROUP_END_MARKER = "# END iptv-pilot user channels"
SUBSCRIPTIONS_BEGIN_MARKER = "# BEGIN iptv-pilot user subscriptions"
SUBSCRIPTIONS_END_MARKER = "# END iptv-pilot user subscriptions"


def atomic_write(path: Path, content: str, mode: Optional[int] = None) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        if mode is not None:
            os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def update_env(path: Path, config_dir: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    replacement = f"IPTV_API_CONFIG_PATH={config_dir}"
    output: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("IPTV_API_CONFIG_PATH="):
            output.append(replacement)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(replacement)
    atomic_write(path, "\n".join(output) + "\n", 0o600)


def install_managed_subscriptions(config_dir: Path, asset: Path) -> None:
    subscribe_file = config_dir / "subscribe.txt"
    current = subscribe_file.read_text(encoding="utf-8") if subscribe_file.exists() else ""
    unmanaged: list[str] = []
    in_managed_block = False
    for line in current.splitlines():
        if line == SUBSCRIPTIONS_BEGIN_MARKER:
            in_managed_block = True
            continue
        if line == SUBSCRIPTIONS_END_MARKER:
            in_managed_block = False
            continue
        if not in_managed_block:
            unmanaged.append(line)

    block = [
        SUBSCRIPTIONS_BEGIN_MARKER,
        *asset.read_text(encoding="utf-8").strip().splitlines(),
        SUBSCRIPTIONS_END_MARKER,
        "",
    ]
    output: list[str] = []
    inserted = False
    for line in unmanaged:
        if not inserted and line.strip() == "[WHITELIST]":
            while output and not output[-1].strip():
                output.pop()
            if output:
                output.append("")
            output.extend(block)
            inserted = True
        output.append(line)
    if not inserted:
        output.extend(["", *block])
    atomic_write(subscribe_file, "\n".join(output).strip() + "\n", 0o640)


def channel_names(asset: Path) -> list[str]:
    names: list[str] = []
    for raw_line in asset.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(",", 1)[0].strip()
        if name:
            names.append(name)
    return names


def parse_owner(value: str) -> tuple[int, int]:
    try:
        uid_text, gid_text = value.split(":", 1)
        uid, gid = int(uid_text), int(gid_text)
    except (ValueError, TypeError) as error:
        raise argparse.ArgumentTypeError("owner must use the UID:GID format") from error
    if uid < 0 or gid < 0:
        raise argparse.ArgumentTypeError("owner UID and GID must be non-negative")
    return uid, gid


def install_managed_channel_group(demo_file: Path, names: list[str]) -> None:
    lines = demo_file.read_text(encoding="utf-8").splitlines()
    start: int | None = None
    end: int | None = None
    group_lines = [GROUP_BEGIN_MARKER, "User channels,#genre#", *names, GROUP_END_MARKER]

    if GROUP_BEGIN_MARKER in lines:
        start = lines.index(GROUP_BEGIN_MARKER)
        if GROUP_END_MARKER in lines[start:]:
            end = start + lines[start:].index(GROUP_END_MARKER) + 1

    if start is not None:
        end = end or len(lines)
        lines[start:end] = group_lines
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(group_lines)

    atomic_write(demo_file, "\n".join(lines).rstrip() + "\n", demo_file.stat().st_mode & 0o777)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install user-provided iptv-api sources")
    parser.add_argument("config_dir", type=Path)
    parser.add_argument(
        "env_file",
        type=Path,
        nargs="?",
        help="optional .env file whose IPTV_API_CONFIG_PATH should be updated",
    )
    parser.add_argument(
        "--channels-file",
        type=Path,
        help="channel file using the 'name,url' format",
    )
    parser.add_argument(
        "--subscriptions-file",
        type=Path,
        help="subscription file containing one user-authorized URL per line",
    )
    parser.add_argument(
        "--owner",
        type=parse_owner,
        help="set managed files to this UID:GID after writing",
    )
    args = parser.parse_args()

    config_dir = args.config_dir.resolve()
    demo_file = config_dir / "demo.txt"
    if not (config_dir / "config.ini").is_file() or not demo_file.is_file():
        raise SystemExit("config_dir must contain the complete iptv-api default config")
    env_file = args.env_file.resolve() if args.env_file else None
    if env_file and not env_file.is_file():
        raise SystemExit("env file does not exist")

    local_dir = config_dir / "local"
    local_dir.mkdir(mode=0o750, exist_ok=True)
    installed: list[Path] = []
    managed_paths: list[Path] = []
    if args.channels_file:
        asset = args.channels_file.resolve()
        if not asset.is_file():
            raise SystemExit(f"channels file does not exist: {asset}")
        names = channel_names(asset)
        if not names:
            raise SystemExit("channels file contains no channel entries")
        destination = local_dir / "iptv_pilot_channels.txt"
        shutil.copyfile(asset, destination)
        os.chmod(destination, 0o640)
        install_managed_channel_group(demo_file, names)
        installed.append(destination)
        managed_paths.extend([local_dir, destination, demo_file])

    if args.subscriptions_file:
        subscriptions = args.subscriptions_file.resolve()
        if not subscriptions.is_file():
            raise SystemExit(f"subscriptions file does not exist: {subscriptions}")
        install_managed_subscriptions(config_dir, subscriptions)
        subscribe_file = config_dir / "subscribe.txt"
        installed.append(subscribe_file)
        managed_paths.append(subscribe_file)

    if env_file:
        update_env(env_file, config_dir)
    if args.owner:
        uid, gid = args.owner
        for path in dict.fromkeys(managed_paths):
            os.chown(path, uid, gid)
    details = ", ".join(str(path) for path in installed) if installed else "configuration path only"
    print(f"iptv-api configured: {details}")


if __name__ == "__main__":
    main()
