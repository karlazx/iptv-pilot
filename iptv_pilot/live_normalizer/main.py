from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from iptv_pilot.common.db import configure_logging, connect_database
from iptv_pilot.common.live_paths import channel_cache_directory

LOG = logging.getLogger(__name__)
STOP = False


def stop_service(_signum, _frame) -> None:
    global STOP
    STOP = True


def clear_channel_cache(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.is_file() and (path.suffix in {".ts", ".m3u8", ".tmp"} or ".tmp" in path.name):
            path.unlink(missing_ok=True)


def ffmpeg_command(url: str, directory: Path) -> list[str]:
    fps = max(1, int(os.getenv("LIVE_NORMALIZE_FPS", "25")))
    width = max(320, int(os.getenv("LIVE_NORMALIZE_WIDTH", "1280")))
    height = max(180, int(os.getenv("LIVE_NORMALIZE_HEIGHT", "720")))
    bitrate = os.getenv("LIVE_NORMALIZE_VIDEO_BITRATE", "3500k")
    maxrate = os.getenv("LIVE_NORMALIZE_VIDEO_MAXRATE", "4000k")
    bufsize = os.getenv("LIVE_NORMALIZE_VIDEO_BUFSIZE", "8000k")
    list_size = max(15, int(os.getenv("LIVE_NORMALIZE_HLS_LIST_SIZE", "30")))
    delete_threshold = max(5, int(os.getenv("LIVE_NORMALIZE_HLS_DELETE_THRESHOLD", "10")))
    start_number = int(time.time() * 1000)
    segment_pattern = str(directory / "segment_%09d.ts")
    manifest = str(directory / "index.m3u8")
    return [
        os.getenv("LIVE_NORMALIZE_FFMPEG_BIN", "/usr/bin/ffmpeg"),
        "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-vaapi_device", os.getenv("LIVE_NORMALIZE_VAAPI_DEVICE", "/dev/dri/renderD128"),
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-thread_queue_size", "1024",
        "-use_wallclock_as_timestamps", "1",
        "-readrate", "1",
        "-i", url,
        "-map", "0:v:0", "-map", "0:a:0?",
        # The CCTV source is 50 fps. Drop it to the target cadence before
        # regenerating timestamps; doing setpts first would make a 50 fps
        # input produce a 2x-fast HLS timeline and force periodic live-edge
        # jumps in the browser.
        "-vf", f"fps={fps},setpts=N/({fps}*TB),format=nv12,hwupload,scale_vaapi=w={width}:h={height}:format=nv12",
        "-fps_mode", "cfr", "-r", str(fps),
        "-c:v", "h264_vaapi", "-profile:v", "high",
        "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize,
        "-g", str(fps * 2), "-keyint_min", str(fps * 2),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        # Start each process from a fresh audio clock and compensate ordinary
        # packet jitter.  The publisher validates every resulting segment and
        # restarts this process if the upstream clock makes a large jump.
        "-af", "aresample=async=1:first_pts=0",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", str(list_size),
        "-start_number", str(start_number),
        "-hls_delete_threshold", str(delete_threshold),
        "-hls_flags", "delete_segments+independent_segments+program_date_time+temp_file",
        "-hls_segment_filename", segment_pattern,
        manifest,
    ]


def manifest_entries(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    if not path.is_file():
        return []
    entries: list[tuple[str, tuple[str, ...]]] = []
    tags: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            tags = [line]
        elif tags and line.startswith("#"):
            tags.append(line)
        elif tags and line:
            entries.append((Path(line).name, tuple(tags)))
            tags = []
    return entries


def segment_sequence(filename: str) -> int:
    return int(Path(filename).stem.rsplit("_", 1)[-1])


class SegmentValidationError(RuntimeError):
    pass


def validate_segment_timeline(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                os.getenv("LIVE_NORMALIZE_FFPROBE_BIN", "/usr/bin/ffprobe"),
                "-v", "error",
                "-show_entries", "stream=codec_type,start_time,duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
        streams = json.loads(result.stdout).get("streams") or []
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SegmentValidationError(f"ffprobe failed for {path.name}: {exc}") from exc
    starts = {
        stream.get("codec_type"): float(stream["start_time"])
        for stream in streams
        if stream.get("start_time") not in {None, "N/A"}
    }
    durations = {
        stream.get("codec_type"): float(stream["duration"])
        for stream in streams
        if stream.get("duration") not in {None, "N/A"}
    }
    if "video" not in starts:
        raise SegmentValidationError(f"segment has no decodable video stream: {path.name}")
    if "audio" not in starts:
        return None
    maximum_duration = max(
        3.0, float(os.getenv("LIVE_NORMALIZE_MAX_TRACK_DURATION_SECONDS", "4.5"))
    )
    for track, duration in durations.items():
        if duration > maximum_duration:
            raise SegmentValidationError(
                f"segment {track} duration {duration:.3f}s exceeds "
                f"{maximum_duration:.3f}s: {path.name}"
            )
    drift = abs(starts["audio"] - starts["video"])
    maximum = max(0.5, float(os.getenv("LIVE_NORMALIZE_MAX_AV_DRIFT_SECONDS", "3")))
    if drift > maximum:
        raise SegmentValidationError(
            f"segment A/V timeline drift {drift:.3f}s exceeds {maximum:.3f}s: {path.name}"
        )
    return drift


class HlsPacer:
    """Expose bursty staging segments at a strict wall-clock cadence."""

    def __init__(self, staging: Path, public: Path) -> None:
        self.staging = staging
        self.public = public
        self.interval = max(1.0, float(os.getenv("LIVE_NORMALIZE_PUBLISH_INTERVAL", "2")))
        self.prebuffer = max(2, int(os.getenv("LIVE_NORMALIZE_PUBLISH_PREBUFFER", "10")))
        self.list_size = max(15, int(os.getenv("LIVE_NORMALIZE_HLS_LIST_SIZE", "30")))
        self.stop_event = threading.Event()
        self.failed = threading.Event()
        self.failure_reason: str | None = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.seen: set[str] = set()
        self.pending: deque[tuple[str, tuple[str, ...]]] = deque()
        self.published: deque[tuple[str, tuple[str, ...]]] = deque()
        self.discontinuity_pending = False
        self.invalid_count = 0
        self.max_invalid = max(2, int(os.getenv("LIVE_NORMALIZE_MAX_INVALID_SEGMENTS", "5")))

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=3)

    def reset_pipeline(self) -> None:
        self.pending.clear()
        self.failed.clear()
        self.failure_reason = None
        self.invalid_count = 0
        self.discontinuity_pending = True

    def discover(self) -> None:
        for entry in manifest_entries(self.staging / "index.m3u8"):
            filename = entry[0]
            if filename not in self.seen:
                self.seen.add(filename)
                self.pending.append(entry)

    def publish(self, entry: tuple[str, tuple[str, ...]]) -> bool:
        filename, tags = entry
        source = self.staging / filename
        if not source.is_file():
            return False
        validate_segment_timeline(source)
        if self.discontinuity_pending:
            tags = ("#EXT-X-DISCONTINUITY",) + tuple(tags)
            self.discontinuity_pending = False
        target = self.public / filename
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        self.published.append((filename, tags))
        while len(self.published) > self.list_size:
            expired, _ = self.published.popleft()
            (self.public / expired).unlink(missing_ok=True)
        self.write_manifest()
        return True

    def write_manifest(self) -> None:
        if not self.published:
            return
        first_sequence = segment_sequence(self.published[0][0])
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            f"#EXT-X-TARGETDURATION:{max(1, round(self.interval))}",
            f"#EXT-X-MEDIA-SEQUENCE:{first_sequence}",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for filename, tags in self.published:
            lines.extend(tags)
            lines.append(filename)
        manifest = self.public / "index.m3u8"
        temporary = manifest.with_suffix(".m3u8.tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, manifest)

    def run(self) -> None:
        next_publish: float | None = None
        while not self.stop_event.is_set():
            try:
                self.discover()
                if next_publish is None and len(self.pending) >= self.prebuffer:
                    next_publish = time.monotonic()
                now = time.monotonic()
                if next_publish is not None and self.pending and now >= next_publish:
                    scheduled_publish = next_publish
                    entry = self.pending.popleft()
                    try:
                        self.publish(entry)
                    except SegmentValidationError as exc:
                        self.invalid_count += 1
                        LOG.error("normalized HLS publisher rejected invalid segment: %s", exc)
                        if self.invalid_count >= self.max_invalid:
                            self.failure_reason = str(exc)
                            self.failed.set()
                            self.invalid_count = 0
                        next_publish = time.monotonic() + self.interval
                        continue
                    self.invalid_count = 0
                    completed = time.monotonic()
                    # Anchor normal publication to the original cadence so
                    # validation/copy overhead does not slowly drain the
                    # player's live buffer. If one publication itself takes
                    # longer than a full interval, reset the clock instead of
                    # catching up in a burst.
                    next_publish = scheduled_publish + self.interval
                    if next_publish <= completed:
                        next_publish = completed + self.interval
            except Exception:
                LOG.exception("normalized HLS publisher failed")
            self.stop_event.wait(0.2)


def consume_ffmpeg_stderr(
    process: subprocess.Popen[str], channel_id: str, counters: dict[str, int]
) -> None:
    if process.stderr is None:
        return
    for raw in process.stderr:
        line = raw.rstrip()
        if not line:
            continue
        lowered = line.lower()
        if "pps" in lowered:
            counters["pps_errors"] += 1
        if "no frame" in lowered:
            counters["no_frame_errors"] += 1
        if "corrupt" in lowered:
            counters["corrupt_packets"] += 1
        LOG.warning("ffmpeg channel=%s %s", channel_id, line)


def segment_timing(directory: Path) -> dict[str, float | int] | None:
    files = sorted(directory.glob("segment_*.ts"), key=lambda item: item.stat().st_mtime)[-20:]
    if len(files) < 3:
        return None
    mtimes = [item.stat().st_mtime for item in files]
    intervals = [mtimes[index] - mtimes[index - 1] for index in range(1, len(mtimes))]
    return {
        "samples": len(intervals),
        "average_seconds": round(sum(intervals) / len(intervals), 3),
        "minimum_seconds": round(min(intervals), 3),
        "maximum_seconds": round(max(intervals), 3),
        "stall_count": sum(value > 3 for value in intervals),
        "burst_count": sum(value < 1 for value in intervals),
    }


def terminate(process: subprocess.Popen | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def run_channel(channel_id: str) -> None:
    database = connect_database()
    root = Path(os.getenv("LIVE_NORMALIZE_ROOT", "/data/media-cache/live-hls"))
    directory = channel_cache_directory(root, channel_id)
    staging = directory / ".staging"
    manifest = directory / "index.m3u8"
    # Cold start drops segments left by an earlier container run so the
    # browser never mixes two unrelated encoder timelines. Restarts inside
    # this process preserve the public buffer below.
    clear_channel_cache(directory)
    clear_channel_cache(staging)
    process: subprocess.Popen | None = None
    source_url = None
    decoder_stats = {"pps_errors": 0, "no_frame_errors": 0, "corrupt_packets": 0}
    next_quality_update = 0.0
    stale_seconds = max(30, int(os.getenv("LIVE_NORMALIZE_STALE_SECONDS", "60")))
    backoff = 2
    pacer = HlsPacer(staging, directory)
    pacer.start()
    try:
        while not STOP:
            channel = database.channels.find_one(
                {"channel_id": channel_id, "enabled": True},
                {"m3u_url": 1, "stream_candidates": 1, "stream_manual": 1},
            ) or {}
            current_url = channel.get("m3u_url")
            if pacer.failed.is_set():
                LOG.error("restarting invalid normalized stream: %s", pacer.failure_reason)
                candidates = channel.get("stream_candidates") or []
                if channel.get("stream_manual") and len(candidates) > 1:
                    current_index = next(
                        (index for index, url in enumerate(candidates) if url == current_url),
                        None,
                    )
                    if current_index is not None:
                        next_index = (current_index + 1) % len(candidates)
                        database.channels.update_one(
                            {"channel_id": channel_id},
                            {
                                "$set": {"m3u_url": candidates[next_index]},
                                "$unset": {"stream_manual": ""},
                            },
                        )
                        LOG.warning(
                            "manual override cleared and failed line switched channel=%s next_candidate=%s",
                            channel_id,
                            next_index,
                        )
                        channel = database.channels.find_one(
                            {"channel_id": channel_id, "enabled": True},
                            {"m3u_url": 1, "stream_candidates": 1, "stream_manual": 1},
                        ) or {}
                        current_url = channel.get("m3u_url")
            output_stale = (
                manifest.exists()
                and time.time() - manifest.stat().st_mtime > stale_seconds
            )
            needs_start = process is None or process.poll() is not None
            needs_restart = bool(
                (current_url and source_url and current_url != source_url)
                or output_stale
                or pacer.failed.is_set()
            )
            if needs_restart:
                terminate(process)
                process = None
                clear_channel_cache(staging)
                # Preserve published segments across the restart so a line
                # switch or an upstream clock jump does not blank the player.
                pacer.reset_pipeline()
            if current_url and (needs_start or needs_restart):
                source_url = current_url
                decoder_stats = {"pps_errors": 0, "no_frame_errors": 0, "corrupt_packets": 0}
                directory.mkdir(parents=True, exist_ok=True)
                staging.mkdir(parents=True, exist_ok=True)
                LOG.info("starting normalized live channel=%s", channel_id)
                pacer.reset_pipeline()
                process = subprocess.Popen(
                    ffmpeg_command(current_url, staging),
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                threading.Thread(
                    target=consume_ffmpeg_stderr,
                    args=(process, channel_id, decoder_stats),
                    daemon=True,
                ).start()
                backoff = 2
            elif not current_url and process is None:
                LOG.warning("waiting for live source channel=%s", channel_id)
            if process is not None and process.poll() is not None:
                LOG.warning("normalizer exited channel=%s code=%s", channel_id, process.returncode)
                terminate(process)
                process = None
                clear_channel_cache(staging)
                pacer.reset_pipeline()
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
            now = time.monotonic()
            if source_url and now >= next_quality_update:
                timing = segment_timing(directory)
                candidates = channel.get("stream_candidates") or []
                source_index = next(
                    (index for index, candidate in enumerate(candidates) if candidate == source_url),
                    None,
                )
                decode_healthy = bool(
                    decoder_stats["pps_errors"] == 0
                    and decoder_stats["no_frame_errors"] == 0
                )
                pacing_healthy = bool(
                    timing
                    and timing["stall_count"] == 0
                    and timing["burst_count"] == 0
                )
                database.channels.update_one(
                    {"channel_id": channel_id},
                    {
                        "$set": {
                            "normalizer_quality": {
                                "measured_at": datetime.now(UTC),
                                "source_index": source_index,
                                "source_host": urlparse(source_url).netloc,
                                # Source selection prioritizes whether frames
                                # can be decoded. Publication jitter is stored
                                # separately because the 20-second browser
                                # buffer is intentionally designed to absorb it.
                                "healthy": decode_healthy,
                                "decode_healthy": decode_healthy,
                                "pacing_healthy": pacing_healthy,
                                "decoder": dict(decoder_stats),
                                "segment_timing": timing,
                            }
                        }
                    },
                )
                next_quality_update = now + 30
            time.sleep(2)
    finally:
        pacer.stop()
        terminate(process)


def main() -> None:
    configure_logging()
    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    channels = [
        value.strip()
        for value in os.getenv("LIVE_NORMALIZE_CHANNELS", "").split(",")
        if value.strip()
    ]
    if not channels:
        raise RuntimeError("LIVE_NORMALIZE_CHANNELS is empty")
    maximum = max(1, int(os.getenv("LIVE_NORMALIZE_MAX_CHANNELS", "2")))
    if len(channels) > maximum:
        raise RuntimeError(
            f"live-normalizer pool limit exceeded: {len(channels)} channels requested, "
            f"maximum is {maximum}"
        )
    workers = [
        threading.Thread(
            target=run_channel,
            args=(channel_id,),
            name=f"normalize-{channel_id}",
            daemon=True,
        )
        for channel_id in channels
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


if __name__ == "__main__":
    main()
