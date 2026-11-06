#!/usr/bin/env python3
"""Measure current HLS candidates without exposing full stream URLs."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from iptv_pilot.common.db import connect_database
from scripts.channels.seed_channels import parse_m3u

HEADERS = {
    "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, video/*, */*",
    "User-Agent": "IPTVPilot/1.0",
}

# An IPTV playlist is a few kilobytes. Anything larger is a video file or an
# error page, and reading it would allocate the whole response in memory: a
# 1.85 GB MP4 mistaken for a playlist exhausted a 1 GB container in testing.
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
# HLS media segments are bounded by the playlist's TARGETDURATION; 64 MiB is a
# generous ceiling that still refuses a mislabelled feature-length video.
MAX_SEGMENT_BYTES = 64 * 1024 * 1024


class PlaylistTooLarge(ValueError):
    """The response is not a playlist and must not be buffered."""


def read_playlist(client: httpx.Client, url: str) -> tuple[str, float]:
    """Stream a playlist with a hard byte cap, returning (text, seconds)."""
    started = time.perf_counter()
    with client.stream("GET", url, headers=HEADERS) as response:
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type.startswith(("video/", "audio/")):
            raise PlaylistTooLarge(f"not a playlist: content-type {content_type}")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_PLAYLIST_BYTES:
            raise PlaylistTooLarge(f"playlist too large: {int(declared)} bytes")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_PLAYLIST_BYTES:
                raise PlaylistTooLarge(f"playlist exceeded {MAX_PLAYLIST_BYTES} bytes")
        elapsed = time.perf_counter() - started
        resolved = str(response.url)
        encoding = response.encoding or "utf-8"
    text = body.decode(encoding, errors="replace")
    if text.lstrip() and not text.lstrip().startswith("#"):
        raise PlaylistTooLarge("response is not an HLS playlist")
    return text, elapsed, resolved


def media_segments(client: httpx.Client, url: str, depth: int = 0):
    text, playlist_seconds, resolved = read_playlist(client, url)
    segments = []
    duration = None
    variants = []
    pending_bandwidth = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-STREAM-INF:"):
            fields = line.split(":", 1)[1].split(",")
            pending_bandwidth = next(
                (int(field.split("=", 1)[1]) for field in fields if field.startswith("BANDWIDTH=")),
                0,
            )
        elif line and not line.startswith("#"):
            absolute = urljoin(resolved, line)
            if duration is not None:
                segments.append((absolute, duration))
                duration = None
            elif pending_bandwidth is not None:
                variants.append((pending_bandwidth, absolute))
                pending_bandwidth = None
    if segments:
        return segments, playlist_seconds, len(variants)
    if variants and depth < 2:
        # Benchmark the highest advertised rendition; it is the most demanding
        # path the browser can select after bandwidth estimation.
        _, child = max(variants, key=lambda item: item[0])
        child_segments, child_seconds, child_variants = media_segments(client, child, depth + 1)
        return child_segments, playlist_seconds + child_seconds, max(len(variants), child_variants)
    raise RuntimeError("playlist contains no media segments")


def benchmark(client: httpx.Client, url: str, sample_count: int) -> dict:
    segments, playlist_seconds, variants = media_segments(client, url)
    samples = []
    for segment_url, duration in segments[-sample_count:]:
        started = time.perf_counter()
        size = 0
        with client.stream("GET", segment_url, headers=HEADERS) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_SEGMENT_BYTES:
                    raise PlaylistTooLarge(
                        f"segment exceeded {MAX_SEGMENT_BYTES} bytes; not a live HLS segment"
                    )
        elapsed = time.perf_counter() - started
        samples.append(
            {
                "duration_seconds": round(duration, 3),
                "download_seconds": round(elapsed, 3),
                "size_mib": round(size / 1048576, 2),
                "download_ratio": round(elapsed / duration, 3),
            }
        )
    total_duration = sum(item["duration_seconds"] for item in samples)
    total_download = sum(item["download_seconds"] for item in samples)
    total_mib = sum(item["size_mib"] for item in samples)
    return {
        "playlist_seconds": round(playlist_seconds, 3),
        "playlist_segments": len(segments),
        "master_variants": variants,
        "samples": samples,
        "required_mbps": round(total_mib * 8 / total_duration, 2),
        "measured_mbps": round(total_mib * 8 / total_download, 2),
        "aggregate_download_ratio": round(total_download / total_duration, 3),
        "sustainable": total_download < total_duration * 0.8,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("channels", nargs="+", help="channel IDs or names")
    parser.add_argument("--m3u", type=Path, default=Path("/data/m3u/result.m3u"))
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()

    database = connect_database()
    rows = list(parse_m3u(args.m3u))
    output = {}
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        for channel in args.channels:
            selected = database.channels.find_one({"channel_id": channel}, {"m3u_url": 1}) or {}
            candidates = [
                row for row in rows if row["channel_id"] == channel or row["name"] == channel
            ]
            results = []
            for index, row in enumerate(candidates, 1):
                result = {
                    "candidate": index,
                    "host": urlparse(row["m3u_url"]).netloc,
                    "selected": selected.get("m3u_url") == row["m3u_url"],
                }
                try:
                    result.update(benchmark(client, row["m3u_url"], max(1, args.samples)))
                except Exception as exc:
                    result["error"] = f"{type(exc).__name__}: {exc}"
                results.append(result)
            output[channel] = results
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
