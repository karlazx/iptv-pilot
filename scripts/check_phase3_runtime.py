#!/usr/bin/env python3
"""Dependency and rule-engine checks that run inside the production image."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import imageio_ffmpeg
from pydantic import ValidationError

from services.scheduler.main import matches
from services.web.main import SubscriptionCreate


def main() -> None:
    programme = {
        "channel_id": "cctv1",
        "title": "新闻联播",
        "description": "国内外新闻",
        "start_time": datetime(2026, 8, 13, 11, 0, tzinfo=UTC),
    }
    assert matches({"match_type": "keyword", "keyword": "新闻"}, programme)
    assert matches({"match_type": "keyword", "keyword": "国内外"}, programme)
    assert matches(
        {
            "match_type": "channel_time",
            "channel_id": "cctv1",
            "time_rule": {
                "weekdays": [3],
                "time": "19:00",
                "timezone": "Asia/Shanghai",
                "tolerance_min": 2,
            },
        },
        programme,
    )
    try:
        SubscriptionCreate(name="bad", match_type="keyword")
    except ValidationError:
        pass
    else:
        raise AssertionError("invalid keyword subscription was accepted")

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ffmpeg, "-version"], check=True, stdout=subprocess.DEVNULL)
    from faster_whisper import WhisperModel  # noqa: F401

    print("PHASE3_RUNTIME_OK rules=3 validation=1 ffmpeg=1 faster_whisper=1")


if __name__ == "__main__":
    main()
