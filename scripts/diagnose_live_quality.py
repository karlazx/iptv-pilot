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
from scripts.seed_channels import parse_m3u

HEADERS = {
    "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, video/*, */*",
    "User-Agent": "IPTVPilot/1.0",
}


def media_segments(client: httpx.Client, url: str, depth: int = 0):
    started = time.perf_counter()
    response = client.get(url, headers=HEADERS)
    response.raise_for_status()
    playlist_seconds = time.perf_counter() - started
    text = response.text
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
            absolute = urljoin(str(response.url), line)
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
        response = client.get(segment_url, headers=HEADERS)
        response.raise_for_status()
        elapsed = time.perf_counter() - started
        samples.append(
            {
                "duration_seconds": round(duration, 3),
                "download_seconds": round(elapsed, 3),
                "size_mib": round(len(response.content) / 1048576, 2),
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
