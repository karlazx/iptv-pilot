"""Ground summaries in source segments and cache unaffected notes."""

import hashlib
import json
import os
from datetime import UTC, datetime


def identify(segments):
    result = []
    for segment in segments:
        s = dict(segment)
        signature = [s.get("part_id", "main"), round(s["start"], 4), round(s["end"], 4), s.get("original_text", s["text"])]
        s.setdefault("id", hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()[:20])
        s.setdefault("part_id", "main")
        s.setdefault("media_start", s["start"])
        s.setdefault("media_end", s["end"])
        result.append(s)
    return result


def setup(database):
    database.analysis_notes.create_index("created_at", expireAfterSeconds=7776000)
    database.transcript_edits.create_index([("recording_id", 1), ("created_at", -1)])
    database.source_recording_events.create_index("created_at", expireAfterSeconds=7776000)


def grounded(provider, segments, notes=None):
    segments = identify(segments)
    payload = {"segments": [{"id": s["id"], "text": s["text"]} for s in segments]}
    if notes:
        payload["notes"] = [{k: n[k] for k in ("summary_text", "key_points", "entities")} for n in notes]
    raw = provider.summarize_grounded(payload) if hasattr(provider, "summarize_grounded") else provider.summarize(json.dumps(payload, ensure_ascii=False))
    result = {k: raw[k] for k in ("summary_text", "key_points", "entities", "follow_up")}
    known = {s["id"]: s for s in segments}
    evidence, seen = [], set()
    for item in raw.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        index, sid, quote = item.get("point_index"), item.get("segment_id"), item.get("quote")
        if type(index) is not int or not isinstance(sid, str) or not 0 <= index < len(result["key_points"]) or sid not in known:
            continue
        s = known[sid]
        if not isinstance(quote, str) or not 0 < len(quote.strip()) <= 600 or quote.strip() not in s.get("source_text", s["text"]):
            continue
        if (index, sid) in seen or sum(e["point_index"] == index for e in evidence) >= 3:
            continue
        seen.add((index, sid))
        evidence.append({"point_index": index, "segment_id": sid, "quote": quote.strip(), **{k: s[k] for k in ("start", "end", "part_id", "media_start", "media_end")}})
    result["evidence"] = evidence
    result["unlocated_points"] = [i for i in range(len(result["key_points"])) if not any(e["point_index"] == i for e in evidence)]
    return result


def cached_note(database, recording, segments, provider):
    segments = identify(segments)
    digest = hashlib.sha256(json.dumps([[(s["id"], s["text"]) for s in segments], os.getenv("LLM_PROVIDER", "mock"), os.getenv("LLM_MODEL", ""), "evidence-v1"], ensure_ascii=False).encode()).hexdigest()
    identity = f"{recording['_id']}:{digest}"
    saved = database.analysis_notes.find_one({"_id": identity})
    if saved:
        return saved
    note = {"_id": identity, "recording_id": recording["_id"], "source_ids": [s["id"] for s in segments], **grounded(provider, segments), "created_at": datetime.now(UTC)}
    database.analysis_notes.update_one({"_id": identity}, {"$setOnInsert": note}, upsert=True)
    return database.analysis_notes.find_one({"_id": identity})


def groups(segments, budget):
    pending, size, bucket = [], 0, None
    for segment in segments:
        current, width = int(segment["start"]//300), len(segment["text"])+200
        if pending and (current != bucket or size+width > budget):
            yield pending
            pending, size = [], 0
        if width > budget:
            for offset in range(0, len(segment["text"]), budget-200):
                yield [{**segment, "text": segment["text"][offset:offset+budget-200]}]
            continue
        pending.append(segment)
        size += width
        bucket = current
    if pending:
        yield pending


def final_summary(database, recording, transcript, provider):
    segments = identify(transcript.get("segments") or [{"start": 0, "end": recording.get("duration_sec", 0), "text": transcript["full_text"]}])
    budget = max(2000, int(os.getenv("LLM_MAX_INPUT_CHARS", "60000")))
    notes = [cached_note(database, recording, group, provider) for group in groups(segments, budget//2)]
    known = {s["id"]: s for s in segments}

    def sources(part):
        quotes = {}
        for note in part:
            for evidence in note.get("evidence", []):
                quotes.setdefault(evidence["segment_id"], set()).add(evidence["quote"])
        # Quotes bound the context size while validation still uses the actual
        # canonical paragraph, never a synthetic concatenation of excerpts.
        return [{**known[sid], "text": "\n".join(sorted(texts)), "source_text": known[sid]["text"]}
                for sid, texts in quotes.items() if sid in known]

    while len(notes) > 1:
        batches, pending, size = [], [], 0
        for note in notes:
            width = len(json.dumps(note, default=str, ensure_ascii=False))+sum(len(s["text"])+200 for s in sources([note]))
            if pending and size+width > budget:
                batches.append(pending)
                pending, size = [], 0
            pending.append(note)
            size += width
        if pending:
            batches.append(pending)
        if len(batches) == len(notes):
            raise RuntimeError("evidence input exceeds budget; increase LLM_MAX_INPUT_CHARS")
        notes = [{**grounded(provider, sources(batch), batch), "source_ids": [s["id"] for s in sources(batch)]} for batch in batches]
    result = {k: v for k, v in notes[0].items() if k not in {"_id", "recording_id", "source_ids", "created_at"}}
    result["transcript_revision"] = transcript.get("revision", 0)
    result["incomplete"] = bool(recording.get("partial"))
    if result["incomplete"]:
        result["summary_text"] = "【录制不完整，仅依据保留内容】"+result["summary_text"]
    return result
