#!/usr/bin/env python3
"""Read push secrets from stdin and atomically update .env without echoing values."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ALLOWED_KEYS = {
    "PUSH_PROVIDER",
    "PUSH_EMAIL_MODE",
    "COZE_BOT_ID",
    "COZE_API_TOKEN",
    "COZE_API_BASE",
    "COZE_USER_ID",
    "COZE_TIMEOUT_SECONDS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_TO",
    "SMTP_USE_SSL",
}


def atomic_update(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    remaining = dict(updates)
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> None:
    env_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env").resolve()
    if not env_path.is_file():
        raise SystemExit(f"environment file not found: {env_path}")
    raw = json.load(sys.stdin)
    if not isinstance(raw, dict) or not raw:
        raise SystemExit("expected a non-empty JSON object")
    unexpected = set(raw).difference(ALLOWED_KEYS)
    if unexpected:
        raise SystemExit(f"unsupported keys: {sorted(unexpected)}")
    updates = {key: str(value).strip() for key, value in raw.items()}
    if any("\n" in value or "\r" in value for value in updates.values()):
        raise SystemExit("environment values must be single-line")
    existing = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            existing[key.strip()] = value.strip()
    effective = {**existing, **updates}
    channels = {
        value.strip().lower()
        for value in effective.get("PUSH_PROVIDER", "none").split(",")
        if value.strip()
    }
    required = set()
    if "email" in channels:
        required.update({"SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_TO"})
    if "coze" in channels:
        required.update({"COZE_BOT_ID", "COZE_API_TOKEN"})
    missing = sorted(key for key in required if not effective.get(key))
    if missing:
        raise SystemExit(f"missing required configuration: {missing}")
    atomic_update(env_path, updates)
    print("push configuration updated; values not displayed; mode=0600")


if __name__ == "__main__":
    main()
