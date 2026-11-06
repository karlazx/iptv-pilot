#!/usr/bin/env python3
"""Continuously prefer sustainable HLS candidates over instant-speed winners."""

from __future__ import annotations

import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from iptv_pilot.common.db import connect_database
from scripts.channels.diagnose_live_quality import HEADERS, benchmark


def safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def assess(url: str, sample_count: int, timeout: float) -> dict:
    result = {"host": urlparse(url).netloc}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=HEADERS) as client:
            measured = benchmark(client, url, sample_count)
        result.update(
            {
                "valid": True,
                "required_mbps": measured["required_mbps"],
                "measured_mbps": measured["measured_mbps"],
                "download_ratio": measured["aggregate_download_ratio"],
                "sustainable": measured["sustainable"],
            }
        )
    except Exception as exc:
        result.update({"valid": False, "sustainable": False, "error": safe_error(exc)})
    return result


def preferred_index(results: list[dict]) -> int | None:
    valid = [(index, row) for index, row in enumerate(results) if row.get("valid")]
    if not valid:
        return None
    return min(
        valid,
        key=lambda item: (
            0 if item[1].get("media_healthy") is True else 2 if item[1].get("media_healthy") is False else 1,
            not item[1].get("sustainable", False),
            float(item[1].get("download_ratio", 100)),
            float(item[1].get("required_mbps", 1000)),
        ),
    )[0]


def should_switch(current: dict | None, best: dict) -> bool:
    if not current or not current.get("valid"):
        return True
    # Runtime decoder/segment telemetry is stronger evidence than an instant
    # bandwidth sample. Do not leave a proven-clean active source for an
    # unobserved candidate, and immediately allow escape from a proven-bad one.
    if current.get("media_healthy") is True and best.get("media_healthy") is not True:
        return False
    if current.get("media_healthy") is False and best.get("media_healthy") is not False:
        return True
    if not best.get("sustainable"):
        return False
    if not current.get("sustainable"):
        return True
    current_ratio = float(current.get("download_ratio", 100))
    best_ratio = float(best.get("download_ratio", 100))
    current_rate = float(current.get("required_mbps", 1000))
    best_rate = float(best.get("required_mbps", 1000))
    return best_ratio <= current_ratio * 0.8 or (
        best_rate <= current_rate * 0.75 and best_ratio <= current_ratio * 1.15
    )


def evaluate_channel(channel: dict, urls: list[str], sample_count: int, timeout: float) -> tuple[dict, list[dict]]:
    return channel, [assess(url, sample_count, timeout) for url in urls]


def run_once(sample_count: int, timeout: float, workers: int) -> tuple[int, int]:
    from iptv_pilot.sources import store
    from iptv_pilot.sources.models import (
        ordered_sources,
        policy_for_channel,
        resolve_active,
    )

    database = connect_database()
    channel_rows = list(
        database.channels.find(
            {"enabled": True, "m3u_url": {"$exists": True}},
            {
                "channel_id": 1,
                "name": 1,
                "m3u_url": 1,
                "normalizer_quality": 1,
                "preferred_source_id": 1,
                "active_source_id": 1,
                "source_policy": 1,
                "stream_manual": 1,
            },
        )
    )
    plans: list[tuple[dict, list[dict]]] = []
    for channel in channel_rows:
        rows = ordered_sources(list(database.sources.find({"channel_id": channel["channel_id"]})))
        if rows:
            plans.append((channel, rows))

    checked = 0
    switched = 0
    measured_at = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(evaluate_channel, channel, [row["url"] for row in rows], sample_count, timeout): (channel, rows)
            for channel, rows in plans
        }
        for future in as_completed(futures):
            channel, planned_rows = futures[future]
            try:
                _, results = future.result()
            except Exception:
                logging.exception("quality benchmark failed channel=%s", channel["channel_id"])
                continue
            sources = list(database.sources.find({"channel_id": channel["channel_id"]}))
            by_id = {row.get("source_id"): row for row in sources}
            ordered = [by_id[row.get("source_id")] for row in planned_rows if row.get("source_id") in by_id]
            if not ordered:
                continue
            results = results[: len(ordered)]
            active = resolve_active(sources, channel)
            runtime_quality = channel.get("normalizer_quality") or {}
            runtime_source_id = runtime_quality.get("source_id")
            measured_at_runtime = runtime_quality.get("measured_at")
            runtime_is_recent = bool(
                measured_at_runtime and (measured_at - measured_at_runtime).total_seconds() <= 7200
            )
            if runtime_is_recent and runtime_source_id:
                for row, result in zip(ordered, results):
                    if row.get("source_id") == runtime_source_id:
                        result["media_healthy"] = runtime_quality.get("healthy")
                        result["decoder"] = runtime_quality.get("decoder")
                        result["segment_timing"] = runtime_quality.get("segment_timing")
                        break

            best_index = preferred_index(results)
            checked += 1
            for row, result in zip(ordered, results):
                store.update_quality(database, row, result)
            database.channels.update_one(
                {"_id": channel["_id"]},
                {
                    "$set": {
                        "stream_quality": {
                            "measured_at": measured_at,
                            "results": [
                                {**result, "source_id": row.get("source_id"), "url": row.get("url")}
                                for row, result in zip(ordered, results)
                            ],
                            "preferred_index": best_index,
                        }
                    }
                },
            )

            if policy_for_channel(channel) != "auto":
                # Locked-primary and manual channels never switch automatically.
                continue
            active_index = next(
                (
                    index
                    for index, row in enumerate(ordered)
                    if row.get("source_id") == (active or {}).get("source_id")
                ),
                None,
            )
            if best_index is None or best_index == active_index:
                continue
            recording_active = database.recordings.count_documents(
                {"channel_id": channel["channel_id"], "status": {"$in": ["queued", "recording"]}},
                limit=1,
            )
            if recording_active:
                continue
            current_result = results[active_index] if active_index is not None else None
            if not should_switch(current_result, results[best_index]):
                continue
            store.activate(
                database,
                channel["channel_id"],
                ordered[best_index]["source_id"],
                temporary=False,
            )
            switched += 1
            logging.info(
                "stream switched channel=%s source=%s ratio=%s required_mbps=%s",
                channel["channel_id"],
                ordered[best_index].get("source_id"),
                results[best_index].get("download_ratio"),
                results[best_index].get("required_mbps"),
            )
    return checked, switched


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    # httpx logs include complete signed stream URLs and overwhelm normal
    # operations. Keep the quality decision summary, not every media request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    interval = max(900, int(os.getenv("STREAM_QUALITY_INTERVAL_SECONDS", "3600")))
    samples = max(1, int(os.getenv("STREAM_QUALITY_SAMPLE_COUNT", "2")))
    timeout = max(5, float(os.getenv("STREAM_QUALITY_TIMEOUT_SECONDS", "20")))
    workers = max(1, min(8, int(os.getenv("STREAM_QUALITY_WORKERS", "3"))))
    while True:
        try:
            checked, switched = run_once(samples, timeout, workers)
            logging.info("stream quality checked=%s switched=%s", checked, switched)
        except Exception:
            logging.exception("stream quality refresh failed")
        if not args.watch:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
