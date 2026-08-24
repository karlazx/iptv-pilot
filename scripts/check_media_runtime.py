#!/usr/bin/env python3
"""Dependency-free web asset and media-safety checks."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from iptv_pilot.web.media import (  # noqa: E402
    MediaPathError,
    prepare_browser_media,
    resolve_recording_file,
)


def main() -> None:
    static = PROJECT_ROOT / "services" / "web" / "static"
    assets = {name: (static / name).read_text(encoding="utf-8") for name in ("index.html", "app.css", "app.js")}
    combined = "\n".join(assets.values())
    assert "http://" not in combined and "https://" not in combined
    for route in ("epg", "subscriptions", "recordings", "summaries", "bookmarks", "system"):
        assert f'data-route="{route}"' in assets["index.html"]
    assert "/api/recordings/${encodeURIComponent(recordingId)}/play" in assets["app.js"]

    with tempfile.TemporaryDirectory(prefix="web-") as directory:
        root = Path(directory) / "recordings"
        root.mkdir()
        mp4 = root / "safe.mp4"
        mp4.write_bytes(b"browser-ready")
        assert resolve_recording_file({"file_path": str(mp4)}, root) == mp4.resolve()
        asset = prepare_browser_media("safe", {"file_path": str(mp4)}, root, Path(directory) / "cache")
        assert asset.path == mp4.resolve() and asset.media_type == "video/mp4" and not asset.generated
        outside = Path(directory) / "outside.ts"
        outside.write_bytes(b"blocked")
        try:
            resolve_recording_file({"file_path": str(outside)}, root)
        except MediaPathError:
            pass
        else:
            raise AssertionError("path confinement check failed")
    print("WEB_RUNTIME_OK pages=6 external_dependencies=0 path_confinement=1")


if __name__ == "__main__":
    main()
