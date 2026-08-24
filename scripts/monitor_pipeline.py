#!/usr/bin/env python3
"""Monitor one real pipeline recording without storing media or generated text."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TERMINAL_FAILURES = {"failed", "asr_failed", "summary_failed"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5190")
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--target-start", required=True)
    parser.add_argument("--deadline", required=True)
    parser.add_argument("--output", default="/tmp/iptv-pilot-pipeline-status.json")
    parser.add_argument("--interval", type=int, default=60)
    return parser.parse_args()


def get_json(base_url: str, path: str, params: dict | None = None) -> object:
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def find_recording(rows: list[dict], subscription_id: str, target_start: datetime) -> dict | None:
    matches = [row for row in rows if row.get("subscription_id") == subscription_id]
    if not matches:
        return None

    def distance(row: dict) -> float:
        value = row.get("start_time") or row.get("scheduled_start")
        if not value:
            return float("inf")
        try:
            return abs((datetime.fromisoformat(value) - target_start).total_seconds())
        except (TypeError, ValueError):
            return float("inf")

    return min(matches, key=distance)


def compact_status(
    subscription_id: str,
    target_start: datetime,
    detail: dict | None,
    error: str | None = None,
) -> dict:
    now = datetime.now().astimezone()
    payload: dict = {
        "checked_at": now.isoformat(),
        "subscription_id": subscription_id,
        "target_start": target_start.isoformat(),
        "state": "awaiting_schedule",
        "terminal": False,
        "success": False,
    }
    if error:
        payload.update({"state": "monitor_error", "error": error})
        return payload
    if not detail:
        return payload

    recording = detail.get("recording") or {}
    transcript = detail.get("transcript") or {}
    summary = detail.get("summary") or {}
    events = detail.get("push_events") or []
    email_events = [event for event in events if event.get("provider") == "email"]
    delivered = any(event.get("status") == "delivered" for event in email_events)
    status = recording.get("status") or "unknown"
    transcript_text = transcript.get("full_text") or ""
    failure = status in TERMINAL_FAILURES
    success = bool(
        status == "summarized"
        and transcript.get("_id")
        and transcript_text.strip()
        and summary.get("_id")
        and summary.get("pushed") is True
        and delivered
    )
    payload.update(
        {
            "state": status,
            "terminal": failure or success,
            "success": success,
            "recording": {
                "id": recording.get("_id"),
                "status": status,
                "channel_id": recording.get("channel_id"),
                "program_title": recording.get("program_title"),
                "start_time": recording.get("start_time"),
                "end_time": recording.get("end_time"),
                "scheduled_start": recording.get("scheduled_start"),
                "scheduled_end": recording.get("scheduled_end"),
                "duration_sec": recording.get("duration_sec"),
                "size_bytes": recording.get("size_bytes"),
                "file_path_present": bool(recording.get("file_path")),
                "error_present": bool(recording.get("error")),
            },
            "transcript": {
                "id": transcript.get("_id"),
                "present": bool(transcript.get("_id")),
                "characters": len(transcript_text),
                "created_at": transcript.get("created_at"),
            },
            "summary": {
                "id": summary.get("_id"),
                "present": bool(summary.get("_id")),
                "provider": summary.get("provider"),
                "model": summary.get("model"),
                "pushed": summary.get("pushed"),
                "push_processed": summary.get("push_processed"),
                "created_at": summary.get("created_at"),
            },
            "email": {
                "delivered": delivered,
                "events": [
                    {
                        "id": event.get("_id"),
                        "status": event.get("status"),
                        "attempts": event.get("attempts"),
                        "updated_at": event.get("updated_at"),
                        "error_present": bool(event.get("error")),
                    }
                    for event in email_events
                ],
            },
        }
    )
    return payload


def main() -> int:
    args = parse_args()
    target_start = datetime.fromisoformat(args.target_start)
    deadline = datetime.fromisoformat(args.deadline)
    if target_start.tzinfo is None or deadline.tzinfo is None:
        raise SystemExit("target-start and deadline must include a UTC offset")
    if args.interval < 10:
        raise SystemExit("interval must be at least 10 seconds")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.with_suffix(f"{output.suffix}.lock")
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("PIPELINE_MONITOR_ALREADY_RUNNING")
            return 0

        while True:
            detail = None
            error = None
            try:
                rows = get_json(args.base_url, "/api/recordings", {"limit": 500})
                recording = find_recording(rows, args.subscription_id, target_start)
                if recording:
                    detail = get_json(args.base_url, f"/api/recordings/{recording['_id']}")
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as exc:
                error = f"{type(exc).__name__}: monitor request failed"

            status = compact_status(args.subscription_id, target_start, detail, error)
            now = datetime.now().astimezone()
            if now >= deadline and not status["terminal"]:
                status.update({"state": "deadline_reached", "terminal": True, "success": False})
            write_atomic(output, status)
            recording_id = (status.get("recording") or {}).get("id") or "none"
            print(
                f"PIPELINE_MONITOR state={status['state']} "
                f"recording={recording_id[-8:]} success={str(status['success']).lower()}",
                flush=True,
            )
            if status["terminal"]:
                return 0 if status["success"] else 1
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
