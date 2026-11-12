"""Versioned incremental notes; previews never enter the push collection."""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument

from iptv_pilot.common.live import merge_chunks, programme_bounds, programme_segments, segments_text
from iptv_pilot.common.runtime import is_draining


def reduce_text(provider, text, maximum=None):
    maximum = max(512, maximum or int(os.getenv("LLM_MAX_INPUT_CHARS", "60000")))
    if len(text) <= maximum:
        return provider.summarize(text)
    notes = [provider.summarize(text[i:i+maximum]) for i in range(0, len(text), maximum)]
    condensed = "\n".join(json.dumps(n, ensure_ascii=False) for n in notes)
    if len(condensed) >= len(text):
        raise RuntimeError("summary reduction did not shrink input; increase input budget")
    return reduce_text(provider, condensed, maximum)


def contiguous(database, row, lane="draft"):
    rows = []
    _, upper = programme_bounds(row)
    for item in database.live_chunks.find({"recording_id": row["_id"],
                "epoch": row["live_epoch"], "lane": lane}).sort("seq", 1):
        if item["seq"] != len(rows) or item["state"] != "done":
            break
        rows.append(item)
        if item["offset"]+item["duration"] >= upper:
            break
    return rows


def make_notes(database, row, provider, final=False, lane="draft"):
    rows = contiguous(database, row, lane)
    previous = list(database.live_notes.find({"recording_id": row["_id"],
                    "epoch": row["live_epoch"], "lane": lane}).sort("first", 1))
    first = previous[-1]["last"] + 1 if previous else 0
    pending = rows[first:]
    target = max(60, int(os.getenv("LIVE_SUMMARY_INTERVAL_SECONDS", "180")))
    while pending:
        group, duration = [], 0
        for chunk in pending:
            group.append(chunk)
            duration += chunk["duration"]
            if duration >= target:
                break
        if duration < target and not final:
            break
        text = segments_text(programme_segments(row, merge_chunks(group)))
        summary = reduce_text(provider, text) if text.strip() else {
            "summary_text": "该时段未检测到可转写语音。", "key_points": [], "entities": [], "follow_up": ""}
        note = {"_id": f"{row['_id']}:{row['live_epoch']}:{lane}:{group[0]['seq']}",
                "recording_id": row["_id"], "epoch": row["live_epoch"],
                "lane": lane,
                "first": group[0]["seq"], "last": group[-1]["seq"],
                "start": group[0]["offset"], "end": group[-1]["offset"]+group[-1]["duration"],
                **summary, "created_at": datetime.now(UTC)}
        database.live_notes.update_one({"_id": note["_id"]}, {"$setOnInsert": note}, upsert=True)
        previous.append(note)
        pending = pending[len(group):]
    return list(database.live_notes.find({"recording_id": row["_id"], "epoch": row["live_epoch"], "lane": lane}).sort("first", 1))


def preview_once(database, provider_factory):
    if is_draining(database, "llm"):
        return
    now = datetime.now(UTC)
    row = database.recordings.find_one_and_update({
        "status": {"$in": ["recording", "completed"]},
        "live_state": {"$in": ["streaming", "sealed"]}, "llm_enabled": {"$ne": False},
        "$and": [{"$or": [{"live_summary_retry_at": None}, {"live_summary_retry_at": {"$lte": now}}]},
                 {"$or": [{"live_summary_lease": None}, {"live_summary_lease": {"$lte": now}}]}],
        "$expr": {"$gte": [{"$subtract": [{"$ifNull": ["$live_transcribed_seconds", 0]},
                                             {"$ifNull": ["$live_summary_seconds", 0]}]},
                            max(60, int(os.getenv("LIVE_SUMMARY_INTERVAL_SECONDS", "180")))]}},
        {"$set": {"live_summary_lease": now+timedelta(minutes=10), "live_summary_claim": uuid.uuid4().hex}},
        sort=[("live_preview_updated_at", 1)], return_document=ReturnDocument.AFTER)
    if not row:
        return
    lease_owner = {"_id": row["_id"], "live_epoch": row["live_epoch"],
                  "live_summary_claim": row["live_summary_claim"]}
    condition = {**lease_owner,
                 "live_state": {"$in": ["streaming", "sealed"]},
                 "summary_state": {"$nin": ["finalizing", "done", "failed"]},
                 "status": {"$in": ["recording", "completed"]}}
    try:
        provider = provider_factory()
        notes = make_notes(database, row, provider)
        if notes:
            database.recordings.update_one(condition, {"$set": {
                "live_preview": {"summary_text": "\n".join(n["summary_text"] for n in notes[-3:]),
                                 "key_points": [p for n in notes[-3:] for p in n["key_points"]],
                                 "draft": True, "through_seconds": notes[-1]["end"]},
                "live_preview_updated_at": datetime.now(UTC), "live_summary_seconds": notes[-1]["end"],
                "summary_state": "preview", "live_summary_error": None}})
        # Verified notes are cached separately; draft recognition never feeds the final.
        make_notes(database, row, provider, lane="verified")
    except Exception as exc:
        database.recordings.update_one(condition, {"$set": {
            "live_summary_error": type(exc).__name__,
            "live_summary_retry_at": datetime.now(UTC)+timedelta(seconds=60)}})
    finally:
        database.recordings.update_one(lease_owner, {"$set": {"live_summary_lease": None}})


def final_summary(database, recording, transcript, provider):
    if transcript.get("live_epoch") and transcript["live_epoch"] == recording.get("live_epoch"):
        notes = make_notes(database, recording, provider, final=True, lane="verified")
        if notes:
            text = "\n".join(f"[{n['start']:.0f}-{n['end']:.0f}s] " +
                             json.dumps({k: n[k] for k in ("summary_text", "key_points", "entities")},
                                        ensure_ascii=False) for n in notes)
            return reduce_text(provider, text)
    return reduce_text(provider, transcript["full_text"])
