"""Search and subtitle rendering on the canonical, corrected transcript."""

import html
import math
import re


def search_segments(segments, query):
    needle = (query or "").strip().casefold()
    return [s for s in segments if not needle or needle in s["text"].casefold()]


def subtitles(segments, kind="srt", part_id=None):
    if kind not in {"srt", "vtt"}:
        raise ValueError("unsupported subtitle format")
    if part_id:
        segments = [s for s in segments if s.get("part_id", "main") == part_id]
    elif any(s.get("part_id", "main") != "main" for s in segments):
        raise ValueError("select a recording part for synchronized subtitles")

    def stamp(value):
        n = max(0, round(value * 1000))
        h, n = divmod(n, 3600000)
        m, n = divmod(n, 60000)
        s, ms = divmod(n, 1000)
        return f"{h:02}:{m:02}:{s:02}{',' if kind == 'srt' else '.'}{ms:03}"

    cues = []
    for segment in segments:
        start = float(segment.get("media_start", segment["start"]))
        end = float(segment.get("media_end", segment["end"]))
        if not math.isfinite(start) or not math.isfinite(end) or end <= max(0, start):
            continue
        # Strip cue separators and control characters from editable text.
        text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", segment["text"])
        text = " ".join(text.split())
        if kind == "vtt":
            text = html.escape(text)
        text = text.replace("-->", "→")
        if text:
            cues.append(f"{len(cues)+1}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
    return ("WEBVTT\n\n" if kind == "vtt" else "") + "\n".join(cues)
