from pathlib import Path

import pytest

from iptv_pilot.web.media import (
    MediaNotFoundError,
    MediaPathError,
    download_media_type,
    prepare_browser_media,
    resolve_recording_file,
)


def test_resolve_recording_file_stays_inside_root(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    media = root / "sample.ts"
    media.write_bytes(b"video")
    assert resolve_recording_file({"file_path": str(media)}, root) == media.resolve()


def test_resolve_recording_file_rejects_outside_root(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    outside = tmp_path / "outside.ts"
    outside.write_bytes(b"video")
    with pytest.raises(MediaPathError):
        resolve_recording_file({"file_path": str(outside)}, root)


def test_resolve_recording_file_requires_existing_path(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    with pytest.raises(MediaNotFoundError):
        resolve_recording_file({"file_path": str(root / "missing.ts")}, root)


def test_mp4_is_served_without_remux(tmp_path: Path):
    root = tmp_path / "recordings"
    root.mkdir()
    media = root / "sample.mp4"
    media.write_bytes(b"browser-ready")
    asset = prepare_browser_media("abc", {"file_path": str(media)}, root, tmp_path / "cache")
    assert asset.path == media.resolve()
    assert asset.media_type == "video/mp4"
    assert asset.generated is False


def test_download_media_types():
    assert download_media_type(Path("video.ts")) == "video/mp2t"
    assert download_media_type(Path("video.mp4")) == "video/mp4"
