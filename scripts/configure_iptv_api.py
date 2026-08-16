#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional


LEGACY_GROUP_MARKER = "# iptv-manager: official international channels"
GROUP_BEGIN_MARKER = "# BEGIN iptv-manager verified international channels"
GROUP_END_MARKER = "# END iptv-manager verified international channels"
DOMESTIC_BEGIN_MARKER = "# BEGIN iptv-manager community domestic subscriptions"
DOMESTIC_END_MARKER = "# END iptv-manager community domestic subscriptions"
GROUP_LINES = [
    GROUP_BEGIN_MARKER,
    "🌍国际频道,#genre#",
    "NHK World-Japan",
    "CNA",
    "BBC News",
    "Al Jazeera English",
    "France 24 English",
    "Arirang TV",
    GROUP_END_MARKER,
]


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
        if line == DOMESTIC_BEGIN_MARKER:
            in_managed_block = True
            continue
        if line == DOMESTIC_END_MARKER:
            in_managed_block = False
            continue
        if not in_managed_block:
            unmanaged.append(line)

    block = [
        DOMESTIC_BEGIN_MARKER,
        *asset.read_text(encoding="utf-8").strip().splitlines(),
        DOMESTIC_END_MARKER,
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


def install_managed_channel_group(demo_file: Path) -> None:
    lines = demo_file.read_text(encoding="utf-8").splitlines()
    start: int | None = None
    end: int | None = None

    if GROUP_BEGIN_MARKER in lines:
        start = lines.index(GROUP_BEGIN_MARKER)
        if GROUP_END_MARKER in lines[start:]:
            end = start + lines[start:].index(GROUP_END_MARKER) + 1
    elif LEGACY_GROUP_MARKER in lines:
        # Older releases appended the managed block at the end without an end marker.
        start = lines.index(LEGACY_GROUP_MARKER)
        end = len(lines)

    if start is not None:
        end = end or len(lines)
        lines[start:end] = GROUP_LINES
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(GROUP_LINES)

    atomic_write(demo_file, "\n".join(lines).rstrip() + "\n", demo_file.stat().st_mode & 0o777)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install persistent, verified iptv-api sources")
    parser.add_argument("config_dir", type=Path)
    parser.add_argument("env_file", type=Path)
    parser.add_argument(
        "--include-community-domestic",
        action="store_true",
        help="install the user-approved, community-maintained domestic subscription list",
    )
    args = parser.parse_args()

    config_dir = args.config_dir.resolve()
    env_file = args.env_file.resolve()
    demo_file = config_dir / "demo.txt"
    if not (config_dir / "config.ini").is_file() or not demo_file.is_file():
        raise SystemExit("config_dir must contain the complete iptv-api default config")
    if not env_file.is_file():
        raise SystemExit("env file does not exist")

    asset = Path(__file__).resolve().parent.parent / "config" / "iptv-api" / "official_international.txt"
    local_dir = config_dir / "local"
    local_dir.mkdir(mode=0o750, exist_ok=True)
    destination = local_dir / asset.name
    shutil.copyfile(asset, destination)
    os.chmod(destination, 0o640)

    install_managed_channel_group(demo_file)

    if args.include_community_domestic:
        domestic_asset = asset.parent / "community_domestic_subscriptions.txt"
        install_managed_subscriptions(config_dir, domestic_asset)

    update_env(env_file, config_dir)
    domestic_status = "enabled" if args.include_community_domestic else "unchanged"
    print(f"iptv-api configured: {destination}; community domestic={domestic_status}")


if __name__ == "__main__":
    main()
