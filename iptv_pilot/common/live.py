"""Read models for independent capture, transcription and summary progress."""

from datetime import UTC, datetime


def segments_text(segments):
    text = ""
    for segment in segments:
        text += segment["text"] if segment.get("word") else (" " if text else "") + segment["text"]
    return text.strip()


def merge_chunks(chunks):
    merged = []
    for chunk in chunks:
        incoming = chunk.get("segments", [])
        previous = merged[-8:]
        for segment in incoming:
            duplicate = segment.get("word") and previous and segment["start"] < previous[-1]["end"]-.05 and any(
                old.get("word") and old["text"].strip().casefold() == segment["text"].strip().casefold()
                and abs(old["start"]-segment["start"]) < .6
                and abs(old["end"]-segment["end"]) < .6 for old in previous)
            if not duplicate:
                merged.append(segment)
    return merged


def core_segments(job, segments):
    offset = job.get("audio_offset", job["offset"])
    first, last = job["offset"], job["offset"]+job["duration"]
    return [{**s, "start": s["start"]+offset, "end": s["end"]+offset} for s in segments
            if s["end"]+offset > first and s["start"]+offset < last]


def programme_bounds(row):
    capture = row.get("capture_started_at")
    if not capture:
        return 0, float("inf")
    lower = max(0, (row.get("start_time", capture)-capture).total_seconds())
    upper = max(lower, (row.get("end_time", capture)-capture).total_seconds())
    return lower, upper


def programme_segments(row, segments):
    lower, upper = programme_bounds(row)
    return [{**s, "start": max(lower, s["start"]), "end": min(upper, s["end"])}
            for s in segments if s["end"] > lower and s["start"] < upper]


def view(database, row):
    epoch = row.get("live_epoch")
    chunks = list(database.live_chunks.find({"recording_id": row["_id"], "epoch": epoch},
                  {"path": 0}).sort("seq", 1)) if epoch else []
    verified_pending = sum(c.get("lane") == "verified" and c["state"] in {"ready", "processing"} for c in chunks)
    chunks = [c for c in chunks if c.get("lane") != "verified"]
    frontier = 0
    for index, chunk in enumerate(chunks):
        if chunk["seq"] != index or chunk["state"] != "done":
            break
        frontier = chunk["offset"] + chunk["duration"]
    audio = row.get("live_audio_seconds", 0)
    now = datetime.now(UTC)
    capturing = row.get("status") == "recording"
    if capturing and row.get("capture_started_at"):
        audio = max(audio, (now-row["capture_started_at"]).total_seconds())
    runtime = row.get("asr_runtime") or {}
    inferred = sum(c.get("inference_seconds", 0) for c in chunks if c["state"] == "done")
    processed = sum(c["duration"] for c in chunks if c["state"] == "done")
    backlog = max(0, audio-frontier)
    _, upper = programme_bounds(row)
    if row.get("asr_state") == "done":
        frontier = max(frontier, row.get("live_verified_seconds", 0))
    if epoch:
        frontier = min(frontier, upper)
        backlog = max(0, min(audio, upper)-frontier)
    capture = "recording" if capturing else ("failed" if row.get("status") == "failed" else
              "waiting" if row.get("status") == "queued" else row.get("capture_state", row.get("status")))
    return {"epoch": epoch, "capture": capture,
            "asr": row.get("asr_state", row.get("status")),
            "summary": row.get("summary_state", row.get("status")),
            "state": row.get("live_state", "disabled"), "through_seconds": frontier,
            "verified_through_seconds": min(row.get("live_verified_seconds", 0), upper),
            "verified_pending_chunks": verified_pending,
            "lag_seconds": round(backlog, 1), "estimated_catchup_seconds": round(backlog*inferred/processed, 1) if processed and inferred else None,
            "pending_chunks": sum(c["state"] in {"ready", "processing"} for c in chunks),
            "runtime": runtime, "preview": row.get("live_preview") if row.get("live_state") != "fallback" else None,
            "updated_at": row.get("live_preview_updated_at"),
            "text": segments_text(programme_segments(row, merge_chunks([c for c in chunks[-40:] if c["state"] == "done"]))) if row.get("live_state") != "fallback" else ""}
