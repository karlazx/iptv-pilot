"""Recording post-processing settings shared by the recorder, encoder and web UI.

Recordings are captured with ``-c copy`` for resilience, then optionally
transcoded to a smaller H.264 MP4 in the background.  The configuration lives
in ``settings.recording_encode`` so it can be changed from the management UI
without recreating containers; environment variables remain the default.
"""

from __future__ import annotations

import os
from typing import Any

ENCODE_KEY = "recording_encode"

DEFAULT_HEIGHT = 720
DEFAULT_CRF = 26
ALLOWED_HEIGHTS = {0, 480, 540, 720, 1080}
CRF_RANGE = (18, 34)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def defaults() -> dict[str, Any]:
    return {
        "enabled": _env_bool("RECORDING_ENCODE_ENABLED", True),
        "height": _env_int("RECORDING_ENCODE_HEIGHT", DEFAULT_HEIGHT),
        "crf": _env_int("RECORDING_ENCODE_CRF", DEFAULT_CRF),
        "preset": os.getenv("RECORDING_ENCODE_PRESET", "veryfast"),
        "keep_original": _env_bool("RECORDING_ENCODE_KEEP_ORIGINAL", False),
    }


def _normalize(value: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    height = int(value.get("height", base["height"]) or 0)
    if height not in ALLOWED_HEIGHTS:
        height = base["height"] if base["height"] in ALLOWED_HEIGHTS else DEFAULT_HEIGHT
    crf = int(value.get("crf", base["crf"]))
    crf = min(CRF_RANGE[1], max(CRF_RANGE[0], crf))
    preset = str(value.get("preset", base["preset"]) or "veryfast")
    if preset not in {"ultrafast", "veryfast", "faster", "fast", "medium", "slow"}:
        preset = base["preset"]
    return {
        "enabled": bool(value.get("enabled", base["enabled"])),
        "height": height,
        "crf": crf,
        "preset": preset,
        "keep_original": bool(value.get("keep_original", base["keep_original"])),
    }


def load_settings(database) -> dict[str, Any]:
    base = defaults()
    if database is None:
        return base
    row = database.settings.find_one({"key": ENCODE_KEY})
    stored = (row or {}).get("value") or {}
    return _normalize(stored, base) if stored else base


def save_settings(database, value: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime

    settings = _normalize(value, defaults())
    database.settings.update_one(
        {"key": ENCODE_KEY},
        {"$set": {"value": settings, "updated_at": datetime.now(UTC)}},
        upsert=True,
    )
    return settings


def output_suffix() -> str:
    return ".mp4"
