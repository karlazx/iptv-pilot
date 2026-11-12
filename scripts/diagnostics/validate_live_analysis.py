"""Replay speech over paced local HTTP into isolated recording/ASR/LLM jobs.

Requires TEST_MONGODB_URI; creates only a uniquely named validation database.
Never invokes push providers or prints transcript/model credentials.
"""

import argparse
import json
import os
import resource
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pymongo import MongoClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--streams", type=int, choices=[1, 2], default=2)
    parser.add_argument("--programme-seconds", type=int, default=90)
    parser.add_argument("--capture-seconds", type=int, default=150)
    parser.add_argument("--summary-interval", type=int, default=60)
    parser.add_argument("--openvino-model", required=True)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--real-llm", action="store_true")
    parser.add_argument("--keep-results", action="store_true")
    args = parser.parse_args()
    if not os.getenv("TEST_MONGODB_URI") or args.capture_seconds < args.programme_seconds+30:
        parser.error("TEST_MONGODB_URI and at least 30 seconds of post-roll are required")
    os.environ.update(LIVE_ASR_ENABLED="1", LIVE_ASR_CHUNK_SECONDS="30",
                      LIVE_SUMMARY_INTERVAL_SECONDS=str(args.summary_interval), ASR_BACKEND="auto", ASR_DEVICE=args.device,
                      ASR_OPENVINO_MODEL=args.openvino_model, ASR_LANGUAGE="zh",
                      RECORDING_ENCODE_ENABLED="false", GCONV_PATH="/nonexistent")
    if not args.real_llm:
        os.environ["LLM_PROVIDER"] = "mock"
    from iptv_pilot.asr import main as asr
    from iptv_pilot.asr.live import setup
    from iptv_pilot.common.live import view
    from iptv_pilot.llm.main import run_once as summarize
    from iptv_pilot.recorder.main import _ffmpeg_executable, process_recording

    directory = Path(tempfile.mkdtemp(prefix="iptv-live-validation-"))
    source = directory/"source.ts"
    command = [_ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "color=c=black:s=320x180:r=5", "-stream_loop", "-1",
               "-i", str(args.audio), "-t", str(args.capture_seconds+20), "-c:v", "mpeg2video",
               "-b:v", "200k", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
               "-muxrate", "500k", "-f", "mpegts", str(source)]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(time.monotonic())
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            started = time.monotonic()
            sent = 0
            try:
                with source.open("rb") as stream:
                    while data := stream.read(6250):
                        self.wfile.write(data)
                        self.wfile.flush()
                        sent += len(data)
                        time.sleep(max(0, started+sent/62500-time.monotonic()))
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = MongoClient(os.environ["TEST_MONGODB_URI"], tz_aware=True)
    database = client["iptv_live_validation_"+uuid.uuid4().hex]
    setup(database)
    database.transcripts.create_index("recording_id", unique=True)
    database.summaries.create_index("recording_id", unique=True)
    stopped = threading.Event()
    errors = []

    def worker(fn, delay):
        while not stopped.is_set():
            try:
                fn(database)
            except Exception as exc:
                errors.append(type(exc).__name__)
            stopped.wait(delay)

    workers = [threading.Thread(target=worker, args=(asr.run_once, 1), daemon=True),
               threading.Thread(target=worker, args=(summarize, 5), daemon=True)]
    captures = []
    metrics = {}
    started = time.monotonic()
    try:
        for i in range(args.streams):
            now = datetime.now(UTC)
            channel = f"live-validation-{uuid.uuid4().hex[:8]}"
            database.channels.insert_one({"channel_id": channel, "enabled": True})
            row = {"channel_id": channel, "program_title": "Live validation", "status": "recording",
                   "start_time": now, "end_time": now+timedelta(seconds=args.programme_seconds),
                   "scheduled_start": now, "scheduled_end": now+timedelta(seconds=args.capture_seconds),
                   "claim_id": uuid.uuid4().hex, "asr_enabled": True, "asr_model": "small",
                   "llm_enabled": True, "push_enabled": False, "live_asr_enabled": True,
                   "source_url": f"http://127.0.0.1:{server.server_port}/stream.ts"}
            row["_id"] = database.recordings.insert_one(row).inserted_id
            metrics[row["_id"]] = {"first_text_seconds": None, "preview_seconds": None,
                                    "final_seconds": None, "capture_seconds": None}

            def capture(row=row):
                try:
                    process_recording(database, row)
                except Exception as exc:
                    errors.append("capture:"+type(exc).__name__)

            captures.append(threading.Thread(target=capture, daemon=True))
        for thread in captures+workers:
            thread.start()
        deadline = started+args.capture_seconds+180
        while time.monotonic() < deadline:
            for rid, metric in metrics.items():
                row = database.recordings.find_one({"_id": rid})
                elapsed = time.monotonic()-started
                if row.get("live_transcribed_seconds", 0) and metric["first_text_seconds"] is None:
                    metric["first_text_seconds"] = elapsed
                if row.get("live_preview") and metric["preview_seconds"] is None:
                    metric["preview_seconds"] = elapsed
                if row.get("summary_state") == "done" and metric["final_seconds"] is None:
                    metric["final_seconds"] = elapsed
                if row.get("capture_state") == "completed" and metric["capture_seconds"] is None:
                    metric["capture_seconds"] = elapsed
            if all(m["capture_seconds"] and m["final_seconds"] for m in metrics.values()):
                break
            time.sleep(.5)
        stopped.set()
        for thread in captures+workers:
            thread.join(timeout=10)
        summaries = database.summaries.count_documents({})
        rows = [database.recordings.find_one({"_id": rid}) for rid in metrics]
        result = {"streams": args.streams, "upstream_requests": len(requests), "errors": errors,
                  "metrics": list(metrics.values()), "final_summaries": summaries,
                  "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
                  "runtime": [row.get("asr_runtime") for row in rows],
                  "live_state": [view(database, row)["state"] for row in rows],
                  "real_llm": args.real_llm}
        args.output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
        assert not errors and summaries == args.streams and len(requests) == args.streams
        assert all(m["first_text_seconds"] and m["preview_seconds"] and m["final_seconds"]
                   and m["first_text_seconds"] < m["capture_seconds"]
                   and m["final_seconds"] < m["capture_seconds"] for m in metrics.values())
        assert all(row.get("live_state") == "done" for row in rows)
    finally:
        stopped.set()
        server.shutdown()
        if not args.keep_results:
            client.drop_database(database.name)
        client.close()


if __name__ == "__main__":
    main()
