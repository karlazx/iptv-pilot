from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class MediaError(RuntimeError):
    """Base class for recording media preparation errors."""


class MediaNotFoundError(MediaError):
    """The recording has no readable media file."""


class MediaPathError(MediaError):
    """The stored path is outside the configured recordings root."""


class MediaPreparationError(MediaError):
    """A browser-playable copy could not be prepared."""


@dataclass(frozen=True)
class MediaAsset:
    path: Path
    media_type: str
    generated: bool = False


def resolve_recording_file(recording: dict, recordings_root: Path) -> Path:
    raw_path = recording.get("file_path")
    if not raw_path:
        raise MediaNotFoundError("recording file is not ready")
    root = recordings_root.resolve()
    candidate = Path(raw_path).resolve()
    if not candidate.is_relative_to(root):
        raise MediaPathError("recording path is outside the media root")
    if not candidate.is_file():
        raise MediaNotFoundError("recording file does not exist")
    return candidate


def _cache_file(recording_id: str, source: Path, cache_dir: Path) -> Path:
    stat = source.stat()
    return cache_dir / f"{recording_id}-{stat.st_size}-{stat.st_mtime_ns}.mp4"


def _prune_cache(cache_dir: Path, maximum_bytes: int, keep: Path) -> None:
    if maximum_bytes <= 0:
        return
    files = [path for path in cache_dir.glob("*.mp4") if path.is_file()]
    total = sum(path.stat().st_size for path in files)
    for path in sorted(files, key=lambda item: item.stat().st_mtime_ns):
        if total <= maximum_bytes:
            break
        if path == keep:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
        except FileNotFoundError:
            continue


def _run_remux(command: list[str], timeout_seconds: int) -> bool:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(30, timeout_seconds),
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def prepare_browser_media(
    recording_id: str,
    recording: dict,
    recordings_root: Path,
    cache_dir: Path,
    *,
    maximum_cache_bytes: int = 20 * 1024**3,
    timeout_seconds: int = 300,
) -> MediaAsset:
    source = resolve_recording_file(recording, recordings_root)
    suffix = source.suffix.lower()
    if suffix in {".mp4", ".m4v", ".mov"}:
        return MediaAsset(source, download_media_type(source))
    if suffix not in {".ts", ".m2ts", ".mts"}:
        raise MediaPreparationError("recording format is not supported for browser playback")

    cache_dir.mkdir(parents=True, exist_ok=True)
    output = _cache_file(recording_id, source, cache_dir)
    lock_path = cache_dir / f".{recording_id}.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if output.is_file() and output.stat().st_size > 1024:
            output.touch()
            return MediaAsset(output, "video/mp4", generated=True)

        try:
            import imageio_ffmpeg

            executable = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as exc:  # pragma: no cover - depends on deployment image
            raise MediaPreparationError("ffmpeg runtime is unavailable") from exc

        temporary = output.with_suffix(".part.mp4")
        temporary.unlink(missing_ok=True)
        command = [
            executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(temporary),
        ]
        succeeded = _run_remux(command, timeout_seconds)
        if not succeeded:
            temporary.unlink(missing_ok=True)
            # Some MPEG-TS recordings contain MP2 audio, and the bundled static
            # ffmpeg can also fail on damaged transport packets. PyAV is already
            # present in the production image through faster-whisper; use it in
            # an isolated process to copy H.264 video and transcode only audio.
            succeeded = _run_remux(
                [sys.executable, "-m", "iptv_pilot.web.remux", str(source), str(temporary)],
                timeout_seconds,
            )
        if not succeeded or not temporary.is_file() or temporary.stat().st_size <= 1024:
            temporary.unlink(missing_ok=True)
            raise MediaPreparationError("recording could not be remuxed for browser playback")
        os.replace(temporary, output)
        for stale in cache_dir.glob(f"{recording_id}-*.mp4"):
            if stale != output:
                stale.unlink(missing_ok=True)
        _prune_cache(cache_dir, maximum_cache_bytes, output)
        return MediaAsset(output, "video/mp4", generated=True)


def download_media_type(path: Path) -> str:
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".ts": "video/mp2t",
        ".m2ts": "video/mp2t",
        ".mts": "video/mp2t",
    }.get(path.suffix.lower(), "application/octet-stream")
