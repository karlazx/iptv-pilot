"""Optimistic transcript edits with a recoverable summary invalidation intent."""

import uuid
from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument

from iptv_pilot.common.live import segments_text
from iptv_pilot.llm.traceable import identify


class EditConflict(ValueError):
    pass


def queue_summary(database, recording_id, revision, notify=False):
    database.recordings.update_one({"_id": recording_id}, {"$set": {
        "status": "asr_done", "asr_state": "done", "summary_state": "waiting",
        "llm_enabled": True,
        "transcript_revision": revision, "summary_retry_count": 0, "next_summary_attempt_at": None,
        "summary_regeneration": True, "push_after_regenerate": notify, "summary_error": None}})


def correct(database, row, segment_id, text, revision, notify=False):
    transcript = database.transcripts.find_one({"recording_id": row["_id"]})
    if not transcript:
        raise KeyError("transcript not found")
    if transcript.get("revision", 0) != revision:
        raise EditConflict("transcript revision changed; reload before editing")
    segments = identify(transcript.get("segments", []))
    target = next((s for s in segments if s["id"] == segment_id), None)
    if target is None:
        raise KeyError("segment not found")
    if target["text"] == text:
        return {"revision": revision, "changed": False}
    now, token = datetime.now(UTC), uuid.uuid4().hex
    locked = database.recordings.find_one_and_update({"_id": row["_id"],
        "status": {"$in": ["asr_done", "summarized", "summary_failed"]},
        "summary_state": {"$ne": "finalizing"}, "$or": [
            {"analysis_edit_lease": None}, {"analysis_edit_lease": {"$lte": now}}]},
        {"$set": {"analysis_edit_claim": token, "analysis_edit_lease": now+timedelta(seconds=120)}},
        return_document=ReturnDocument.AFTER)
    if not locked:
        raise EditConflict("capture or analysis is still active; retry after completion")
    old_summary = database.summaries.find_one({"recording_id": row["_id"]})
    intent = {"_id": token, "recording_id": row["_id"], "segment_id": segment_id,
              "before": target["text"], "after": text, "from_revision": revision,
              "to_revision": revision+1, "notify": notify, "state": "prepared",
              "old_summary": old_summary, "created_at": now}
    committed = False
    try:
        database.transcript_edits.insert_one(intent)
        if old_summary:
            database.summary_versions.update_one({"recording_id": row["_id"], "generation": old_summary.get("generation", 0)},
                {"$setOnInsert": {"document": old_summary, "created_at": now}}, upsert=True)
            database.summaries.update_one({"_id": old_summary["_id"]}, {"$set": {
                "ready_for_push": False, "push_processed": True, "outdated": True}})
        target.setdefault("original_text", target["text"])
        target.update(text=text, edited_at=now)
        query = {"_id": transcript["_id"], "$or": [{"revision": revision}] + ([{"revision": None}] if revision == 0 else [])}
        saved = database.transcripts.update_one(query, {"$set": {
            "segments": segments, "full_text": segments_text(segments), "updated_at": now,
            "revision": revision+1}, "$unset": {"embedding": ""}})
        if not saved.matched_count:
            raise EditConflict("transcript revision changed; reload before editing")
        committed = True
        queue_summary(database, row["_id"], revision+1, notify)
        database.transcript_edits.update_one({"_id": token}, {"$set": {"state": "applied"}})
        return {"revision": revision+1, "changed": True, "summary_state": "waiting"}
    finally:
        if not committed and old_summary:
            restore_summary(database, old_summary)
            database.transcript_edits.update_one({"_id": token}, {"$set": {"state": "aborted"}})
        database.recordings.update_one({"_id": row["_id"], "analysis_edit_claim": token},
                                       {"$set": {"analysis_edit_lease": None}})


def restore_summary(database, saved):
    values = {k: saved.get(k) for k in ["ready_for_push", "push_processed", "outdated"]}
    database.summaries.update_one({"_id": saved["_id"], "generation": saved.get("generation")}, {"$set": values})


def regenerate(database, row):
    now, token = datetime.now(UTC), uuid.uuid4().hex
    locked = database.recordings.find_one_and_update({"_id": row["_id"],
        "status": {"$in": ["asr_done", "summarized", "summary_failed"]}, "summary_state": {"$ne": "finalizing"},
        "$or": [{"analysis_edit_lease": None}, {"analysis_edit_lease": {"$lte": now}}]},
        {"$set": {"analysis_edit_claim": token, "analysis_edit_lease": now+timedelta(seconds=120)}})
    if not locked:
        raise EditConflict("analysis is active")
    try:
        transcript = database.transcripts.find_one({"recording_id": row["_id"]})
        if not transcript:
            raise KeyError("transcript not found")
        revision = transcript.get("revision", 0)
        saved = database.summaries.find_one({"recording_id": row["_id"]})
        database.transcript_edits.insert_one({"_id": token, "recording_id": row["_id"],
            "kind": "regenerate", "from_revision": revision, "to_revision": revision,
            "old_summary": saved, "state": "prepared", "created_at": now})
        if saved:
            database.summary_versions.update_one({"recording_id": row["_id"], "generation": saved.get("generation", 0)},
                {"$setOnInsert": {"document": saved, "created_at": now}}, upsert=True)
            database.summaries.update_one({"_id": saved["_id"]}, {"$set": {"ready_for_push": False, "push_processed": True, "outdated": True}})
        queue_summary(database, row["_id"], revision)
        database.transcript_edits.update_one({"_id": token}, {"$set": {"state": "applied"}})
        return {"summary_state": "waiting", "revision": revision}
    finally:
        database.recordings.update_one({"_id": row["_id"], "analysis_edit_claim": token}, {"$set": {"analysis_edit_lease": None}})


def repair(database):
    for edit in database.transcript_edits.find({"state": "prepared"}).limit(10):
        row = database.recordings.find_one({"_id": edit["recording_id"]})
        if row and row.get("analysis_edit_lease") and row["analysis_edit_lease"] > datetime.now(UTC):
            continue
        transcript = database.transcripts.find_one({"recording_id": edit["recording_id"]}) or {}
        if transcript.get("revision", 0) == edit["to_revision"]:
            queue_summary(database, edit["recording_id"], edit["to_revision"], edit.get("notify", False))
            state = "applied"
        else:
            if edit.get("old_summary"):
                restore_summary(database, edit["old_summary"])
            state = "aborted"
        database.transcript_edits.update_one({"_id": edit["_id"]}, {"$set": {"state": state}})
