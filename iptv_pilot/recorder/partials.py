"""Preserve sealed attempts without stitching sources with different delays."""

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path


def report_source(database, recording, success, duration=0):
    source_id = recording.get("source_id")
    if not source_id:
        return
    identity = f"{recording['_id']}:{recording.get('claim_id')}"
    result = database.source_recording_events.update_one({"_id": identity}, {"$setOnInsert": {
        "source_id": source_id, "success": success, "duration": duration, "created_at": datetime.now(UTC)}}, upsert=True)
    if not result.upserted_id:
        return
    events = list(database.source_recording_events.find({"source_id": source_id}).sort("created_at", -1).limit(20))
    failures = sum(not e["success"] for e in events)
    update = {"recording_health": {"attempts": len(events), "failures": failures,
                                   "score": round(100*(len(events)-failures)/len(events)),
                                   "last_observed_at": datetime.now(UTC)}}
    if not success:
        update.update(last_fail_at=datetime.now(UTC), last_error="recording stream interrupted or incomplete")
    database.sources.update_one({"source_id": source_id}, {"$set": update,
        "$inc": {"recording_failures_total": int(not success)}})


def part_metadata(recording, path, started, duration, method):
    origin = recording.get("timeline_origin") or recording.get("capture_started_at") or started
    return {"id": uuid.uuid4().hex, "path": str(path), "started_at": started,
            "ended_at": datetime.now(UTC), "offset": max(0, (started-origin).total_seconds()),
            "duration": duration, "size_bytes": Path(path).stat().st_size,
            "source_id": recording.get("source_id"), "epoch": recording.get("claim_id"),
            "method": method, "sealed": True}


def save_partial(database, recording, path, started, method="interrupted"):
    from iptv_pilot.recorder.main import probe_media_duration

    path = Path(path)
    if not path.resolve().is_relative_to(Path(os.getenv("RECORDINGS_ROOT", "/data/recordings")).resolve()):
        raise ValueError("partial recording is outside recordings root")
    if not path.is_file() or path.stat().st_size < int(os.getenv("RECORDING_PARTIAL_MIN_BYTES", "262144")):
        return None
    saved = path.with_name(path.stem+".saved-"+uuid.uuid4().hex[:12]+".ts")
    if method == "worker_lost":
        # The old process may still be alive during a heartbeat partition.
        # Copy a bounded prefix instead of renaming a file it can keep growing.
        remaining = path.stat().st_size
        with path.open("rb") as source, saved.open("wb") as output:
            while remaining:
                data = source.read(min(1048576, remaining))
                if not data:
                    break
                output.write(data)
                remaining -= len(data)
        duration = probe_media_duration(saved)
        if duration is None or duration < float(os.getenv("RECORDING_PARTIAL_MIN_SECONDS", "5")):
            saved.unlink(missing_ok=True)
            return None
    else:
        duration = probe_media_duration(path)
        if duration is None or duration < float(os.getenv("RECORDING_PARTIAL_MIN_SECONDS", "5")):
            return None
        path.replace(saved)
    part = part_metadata(recording, saved, started, duration, method)
    result = database.recordings.update_one({"_id": recording["_id"], "claim_id": recording.get("claim_id"),
        "recording_parts": {"$not": {"$elemMatch": {"epoch": recording.get("claim_id"), "method": method}}}},
        {"$push": {"recording_parts": part}, "$set": {"partial": True, "has_saved_parts": True,
            "timeline_origin": recording.get("timeline_origin") or started,
            "live_state": "fallback", "asr_state": "waiting", "summary_state": "waiting"}})
    if result.matched_count:
        recording.setdefault("recording_parts", []).append(part)
        recording["partial"] = True
        return part
    if database.recordings.find_one({"_id": recording["_id"], "recording_parts": {"$elemMatch": {
            "epoch": recording.get("claim_id"), "method": method}}}):
        saved.unlink(missing_ok=True)
    return None


def publish_partial(database, recording, reason):
    row = database.recordings.find_one({"_id": recording["_id"], "claim_id": recording.get("claim_id")})
    if not row or not row.get("recording_parts"):
        return False
    parts = row["recording_parts"]
    readable = [p for p in parts if Path(p["path"]).is_file()]
    if not readable:
        return False
    latest = readable[-1]
    database.recordings.update_one({"_id": recording["_id"], "claim_id": recording.get("claim_id")}, {"$set": {
        "status": "completed", "capture_state": "partial", "capture_error": str(reason)[:500],
        "partial": True, "has_saved_parts": True, "file_path": latest["path"],
        "asr_source_path": latest["path"], "duration_sec": round(sum(p["duration"] for p in readable)),
        "size_bytes": sum(p["size_bytes"] for p in readable), "live_state": "fallback", "asr_state": "waiting",
        "summary_state": "waiting", "completed_at": datetime.now(UTC), "next_asr_attempt_at": None,
        "encode_status": "pending", "next_encode_attempt_at": None, "keep_original": True}})
    return True


def coverage(recording):
    parts = sorted(recording.get("recording_parts", []), key=lambda p: p["offset"])
    origin = recording.get("timeline_origin") or recording.get("capture_started_at")
    missing, cursor, covered = [], 0.0, 0.0
    lower = max(0, (recording.get("start_time", origin)-origin).total_seconds()) if origin else 0
    upper = max(lower, (recording.get("end_time", origin)-origin).total_seconds()) if origin else 0
    cursor = lower
    for part in parts:
        first, last = max(lower, part["offset"]), min(upper, part["offset"]+part["duration"])
        if last <= first:
            continue
        if first > cursor+.5:
            missing.append({"start": cursor, "end": first})
        covered += max(0, last-max(cursor, first))
        cursor = max(cursor, last)
    if upper > cursor+.5:
        missing.append({"start": cursor, "end": upper})
    return {"incomplete": bool(recording.get("partial")), "missing_intervals": missing,
            "covered_seconds": round(covered, 2), "timeline_seconds": upper,
            "parts": [{k: v for k, v in p.items() if k != "path"} for p in parts]}
