"""Isolated media/ASR/encoder/correction acceptance; never invokes push providers."""

import argparse
import json
import os
import shutil
import subprocess
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import MongoClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--keep-results", action="store_true")
    args = parser.parse_args()
    if not os.getenv("TEST_MONGODB_URI"):
        parser.error("TEST_MONGODB_URI required; production database is never used")
    from iptv_pilot.asr import main as asr
    from iptv_pilot.common.media_refs import reap_originals
    from iptv_pilot.llm import corrections
    from iptv_pilot.llm import main as llm
    from iptv_pilot.llm.providers import MockProvider
    from iptv_pilot.recorder import encoder
    from iptv_pilot.recorder import main as recorder
    from iptv_pilot.recorder.partials import coverage
    from iptv_pilot.web.media import prepare_browser_media

    os.environ.update(LIVE_ASR_ENABLED="0", ASR_BACKEND="auto", ASR_LANGUAGE="zh",
                      EMBEDDING_BACKEND="none", RECORDING_ENCODE_ENABLED="true")
    if not args.real_llm:
        os.environ["LLM_PROVIDER"] = "mock"
    root = Path("/data/recordings")/('phase2-validation-'+uuid.uuid4().hex)
    root.mkdir(parents=True)
    source = root/"fixture.ts"
    subprocess.run([recorder._ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=5", "-stream_loop", "-1", "-i", str(args.audio),
        "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-muxrate", "500k", "-f", "mpegts", str(source)], check=True)
    client = MongoClient(os.environ["TEST_MONGODB_URI"], tz_aware=True)
    db = client['iptv_phase2_acceptance_'+uuid.uuid4().hex]
    db.transcripts.create_index("recording_id", unique=True)
    db.summaries.create_index("recording_id", unique=True)
    token = uuid.uuid4().hex
    now = datetime.now(UTC)
    row = {"channel_id": token, "program_title": "Phase two validation", "status": "recording",
           "claim_id": token, "source_url": "http://example.invalid/isolated-fixture", "asr_enabled": True,
           "asr_model": "small", "live_asr_enabled": False, "llm_enabled": True, "push_enabled": False,
           "start_time": now, "end_time": now+timedelta(seconds=30), "scheduled_end": now+timedelta(seconds=30)}
    db.channels.insert_one({"channel_id": token, "enabled": True})
    row["_id"] = db.recordings.insert_one(row).inserted_id
    original_capture, original_window = recorder.capture_stream, asr.transcribe_window
    recorder.capture_stream = lambda url, path, end, **kwargs: (shutil.copy2(source, path) and "fixture")
    result = {}
    try:
        recorder.process_recording(db, row)
        captured = db.recordings.find_one({"_id": row["_id"]})
        original = Path(captured["file_path"])
        entered, proceed = threading.Event(), threading.Event()

        def delayed_window(audio, model):
            entered.set()
            assert proceed.wait(60), "encoder did not finish during the ASR lease"
            return original_window(audio, model)

        asr.transcribe_window = delayed_window
        job = asr.claim_job(db)
        thread = threading.Thread(target=asr.process_recording, args=(db, job), daemon=True)
        thread.start()
        assert entered.wait(60)
        encoded = encoder.claim_job(db)
        assert encoded and db.recordings.find_one({"_id": row["_id"]})["status"] == "asr_processing"
        encoder.encode_recording(db, encoded, {"height": 90, "crf": 28, "preset": "ultrafast", "keep_original": False})
        saved = db.recordings.find_one({"_id": row["_id"]})
        assert saved["encode_status"] == "done" and original.exists()
        result["encoder_finished_during_asr"] = True
        proceed.set()
        thread.join(timeout=180)
        assert not thread.is_alive()
        saved = db.recordings.find_one({"_id": row["_id"]})
        assert saved["status"] == "asr_done"
        reap_originals(db)
        assert not original.exists() and Path(saved["file_path"]).exists()
        result["original_retired_after_asr"] = True
        llm.run_once(db)
        summary = db.summaries.find_one({"recording_id": row["_id"]})
        assert summary and summary["evidence"] and summary["push_enabled"] is False
        result["verified_evidence_count"] = len(summary["evidence"])
        transcript = db.transcripts.find_one({"recording_id": row["_id"]})
        segment = transcript["segments"][0]
        corrections.correct(db, db.recordings.find_one({"_id": row["_id"]}), segment["id"], segment["text"]+" ", 0)
        llm.run_once(db)
        updated = db.summaries.find_one({"recording_id": row["_id"]})
        assert updated["transcript_revision"] == 1 and updated["generation"] > summary["generation"]
        assert updated["push_enabled"] is False and updated["evidence"]
        result["correction_regenerated_without_push"] = True
        result["runtime"] = transcript.get("asr_runtime")

        # A short valid attempt must survive final failure and remain playable.
        os.environ.update(ASR_BACKEND="mock", RECORDING_SOURCE_RETRIES="0", RECORDING_MAX_SOURCE_SWITCHES="0")
        llm.create_provider = MockProvider
        partial_now = datetime.now(UTC)
        partial = {**row, "_id": None, "claim_id": uuid.uuid4().hex, "start_time": partial_now,
                   "timeline_origin": partial_now,
                   "end_time": partial_now+timedelta(seconds=120), "scheduled_end": partial_now+timedelta(seconds=120)}
        partial.pop("_id")
        partial["_id"] = db.recordings.insert_one(partial).inserted_id
        try:
            recorder.process_recording(db, partial)
        except RuntimeError as exc:
            recorder.handle_failure(db, partial, exc)
        saved = db.recordings.find_one({"_id": partial["_id"]})
        assert saved["capture_state"] == "partial" and saved["recording_parts"]
        prepare_browser_media(str(partial["_id"]), saved, Path("/data/recordings"), root/"cache")
        asr.run_once(db)
        llm.run_once(db)
        summary = db.summaries.find_one({"recording_id": partial["_id"]})
        assert summary and summary["incomplete"] and summary["summary_text"].startswith("【录制不完整")
        result["partial_media_and_summary_preserved"] = True
        result["missing_intervals"] = coverage(saved)["missing_intervals"]
        result["real_llm"] = args.real_llm
        if args.keep_results:
            result.update(validation_database=db.name, recording_id=str(row["_id"]), partial_recording_id=str(partial["_id"]))
        args.output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        recorder.capture_stream, asr.transcribe_window = original_capture, original_window
        if not args.keep_results:
            client.drop_database(db.name)
            for path in [root, Path("/data/recordings")/token]:
                assert path.resolve().parent == Path("/data/recordings").resolve()
                if path.exists():
                    shutil.rmtree(path)
        client.close()


if __name__ == "__main__":
    main()
