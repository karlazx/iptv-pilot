#!/usr/bin/env python3
"""Add non-secret web media-cache settings to an existing .env file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env_path = Path(args.env_file)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    recordings = values.get("RECORDINGS_PATH", "").strip()
    if not recordings:
        raise SystemExit("RECORDINGS_PATH is required")
    media_cache = str(Path(recordings).parent / "media-cache")
    updates = {
        "MEDIA_CACHE_PATH": media_cache,
        "MEDIA_CACHE_MAX_BYTES": values.get("MEDIA_CACHE_MAX_BYTES", "21474836480") or "21474836480",
        "MEDIA_REMUX_TIMEOUT_SECONDS": values.get("MEDIA_REMUX_TIMEOUT_SECONDS", "300") or "300",
    }
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    print("WEB_ENV_CONFIGURED media_cache=true secrets_printed=false")


if __name__ == "__main__":
    main()
