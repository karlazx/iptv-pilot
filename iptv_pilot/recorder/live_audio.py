"""Best-effort local audio sidecar; never backpressure the network recorder."""

import logging
import os
import shutil
import subprocess
import threading
import wave
from datetime import UTC, datetime
from pathlib import Path

from iptv_pilot.common.live import programme_bounds

LOG = logging.getLogger(__name__)
ROOT = Path("/data/live-audio")


class ContextSegmenter:
    """Sliding context with nonoverlapping commit ranges and right lookahead."""

    def __init__(self, window=30):
        window = max(10, min(60, window))
        self.context = min(5, window//6)*32000
        self.core = window*32000-2*self.context
        self.buffer = bytearray()
        self.left = 0
        self.offset = 0

    def feed(self, pcm, final=False):
        self.buffer.extend(pcm)
        ready = []
        while len(self.buffer) >= self.left+self.core+self.context:
            width = self.left+self.core+self.context
            ready.append({"pcm": bytes(self.buffer[:width]), "offset": self.offset/32000,
                          "duration": self.core/32000, "audio_offset": (self.offset-self.left)/32000})
            remove = self.left+self.core-self.context
            del self.buffer[:remove]
            self.left = self.context
            self.offset += self.core
        if final and len(self.buffer) > self.left:
            ready.append({"pcm": bytes(self.buffer), "offset": self.offset/32000,
                          "duration": (len(self.buffer)-self.left)/32000,
                          "audio_offset": (self.offset-self.left)/32000})
            self.buffer.clear()
        return ready


class VerifiedSegmenter:
    """Preserve the offline ASR window size; flush the tail at programme end."""

    def __init__(self, seconds=300, upper=float("inf")):
        self.width = max(30, min(600, seconds))*32000
        self.upper = round(upper*32000) if upper != float("inf") else None
        self.offset = 0
        self.buffer = bytearray()
        self.sealed = False

    def feed(self, pcm, final=False):
        if self.sealed:
            return []
        if self.upper is not None:
            pcm = pcm[:max(0, self.upper-self.offset-len(self.buffer))]
        self.buffer.extend(pcm)
        ready = []
        ending = final or (self.upper is not None and self.offset+len(self.buffer) >= self.upper)
        while len(self.buffer) >= self.width or (ending and self.buffer):
            width = min(self.width, len(self.buffer))
            ready.append({"pcm": bytes(self.buffer[:width]), "offset": self.offset/32000,
                          "duration": width/32000, "audio_offset": self.offset/32000})
            del self.buffer[:width]
            self.offset += width
        self.sealed = ending
        return ready


class LiveAudio:
    def __init__(self, database, recording, path, executable):
        self.db, self.row, self.path, self.executable = database, recording, path, executable
        self.epoch = recording["claim_id"]
        self.filter = {"_id": recording["_id"], "claim_id": self.epoch, "live_epoch": self.epoch}
        self.stop = threading.Event()
        self.process = None
        self.failure = None
        self.count = 0
        self.verified_count = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.db.recordings.update_one({"_id": self.row["_id"], "claim_id": self.epoch}, {"$set": {
            "live_epoch": self.epoch, "live_state": "streaming", "asr_state": "streaming",
            "capture_state": "recording", "summary_state": "waiting", "live_chunk_count": 0,
            "live_transcribed_seconds": 0, "live_notes_last_seq": -1,
            "live_verified_count": 0, "live_verified_seconds": 0,
            "live_audio_seconds": 0, "live_summary_seconds": 0,
            "live_summary_lease": None, "live_summary_claim": None,
            "live_summary_retry_at": None, "live_summary_error": None, "live_error": None,
            "summary_claim": None, "summary_heartbeat_at": None,
            "summary_retry_count": 0, "next_summary_attempt_at": None,
            "live_preview": None, "live_preview_updated_at": None,
        }})
        self.thread.start()

    def finish(self, success):
        self.stop.set()
        self.thread.join(timeout=20)
        if self.thread.is_alive():
            self.failure = "audio sidecar did not finish"
            if self.process:
                self.process.kill()
            self.thread.join(timeout=3)
        state = "sealed" if success and not self.failure else "fallback"
        self.db.recordings.update_one(self.filter, {"$set": {
            "live_chunk_count": self.count,
            "live_sealed_at": datetime.now(UTC), "live_error": self.failure,
        }})
        self.db.recordings.update_one({**self.filter, "live_state": "streaming"},
                                      {"$set": {"live_state": state}})

    def feed(self):
        try:
            while not self.path.exists():
                if self.stop.wait(.1):
                    return
            with self.path.open("rb") as source:
                identity = os.fstat(source.fileno()).st_ino
                while True:
                    current = self.path.stat()
                    if current.st_ino != identity or current.st_size < source.tell():
                        raise RuntimeError("capture file replaced; use offline transcription")
                    data = source.read(65536)
                    if data:
                        self.process.stdin.write(data)
                    elif self.stop.wait(.1):
                        break
        except Exception as exc:
            self.failure = type(exc).__name__
        finally:
            try:
                self.process.stdin.close()
            except (OSError, BrokenPipeError):
                pass

    def publish(self, pcm, offset, duration, audio_offset, lane="draft"):
        directory = ROOT / str(self.row["_id"]) / self.epoch
        directory.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(directory).free < int(os.getenv("LIVE_AUDIO_MIN_FREE_BYTES", "1073741824")):
            raise RuntimeError("insufficient live audio cache space")
        sequence = self.verified_count if lane == "verified" else self.count
        path = directory / f"{lane}-{sequence:06d}.wav"
        temporary = path.with_suffix(".part")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(pcm)
        temporary.replace(path)
        document = {"_id": f"{self.row['_id']}:{self.epoch}:{lane}:{sequence}",
                    "recording_id": self.row["_id"], "epoch": self.epoch, "seq": sequence,
                    "lane": lane, "priority": 0 if lane == "verified" else 1,
                    "path": str(path), "offset": offset, "duration": duration,
                    "audio_offset": audio_offset,
                    "state": "ready", "attempts": 0, "created_at": datetime.now(UTC)}
        self.db.live_chunks.update_one({"_id": document["_id"]}, {"$setOnInsert": document}, upsert=True)
        if lane == "verified":
            self.verified_count += 1
            self.db.recordings.update_one(self.filter, {"$set": {"live_verified_count": self.verified_count}})
            return
        self.count += 1
        self.db.recordings.update_one(self.filter, {"$set": {
            "live_chunk_count": self.count, "live_audio_seconds": offset + duration,
            "live_audio_at": datetime.now(UTC),
        }})

    def run(self):
        try:
            seconds = max(10, min(60, int(os.getenv("LIVE_ASR_CHUNK_SECONDS", "30"))))
            command = [self.executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                       "-threads", "1", "-filter_threads", "1", "-f", "mpegts",
                       "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "16000",
                       "-f", "s16le", "pipe:1"]
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL)
            feeder = threading.Thread(target=self.feed, daemon=True)
            feeder.start()
            segmenter = ContextSegmenter(seconds)
            verified = VerifiedSegmenter(int(os.getenv("ASR_CHUNK_SECONDS", "300")),
                                         programme_bounds(self.row)[1])
            while data := self.process.stdout.read(32000):
                for part in segmenter.feed(data):
                    self.publish(**part)
                for part in verified.feed(data):
                    self.publish(**part, lane="verified")
            for part in segmenter.feed(b"", final=True):
                self.publish(**part)
            for part in verified.feed(b"", final=True):
                self.publish(**part, lane="verified")
            self.process.wait(timeout=10)
            feeder.join(timeout=2)
            if self.process.returncode or self.failure:
                raise RuntimeError("audio decode failed")
        except Exception as exc:
            self.failure = type(exc).__name__
            LOG.warning("live audio unavailable recording_id=%s; offline ASR retained", self.row["_id"])
            try:
                self.db.recordings.update_one(self.filter, {"$set": {"live_state": "fallback", "live_error": self.failure}})
            except Exception:
                LOG.warning("could not publish live audio fallback state")
        finally:
            if self.process and self.process.poll() is None:
                self.process.kill()
