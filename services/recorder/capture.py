"""Isolated PyAV fallback for recording unstable live streams as MPEG-TS."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import av


def capture(source_url: str, output_path: Path, duration_seconds: int) -> None:
    input_options = {
        "rw_timeout": "15000000",
        "reconnect": "1",
        "reconnect_streamed": "1",
        "reconnect_delay_max": "5",
    }
    source = av.open(source_url, mode="r", options=input_options)
    output = av.open(str(output_path), mode="w", format="mpegts")
    mapping = {}
    try:
        for stream in source.streams:
            if stream.type not in {"video", "audio"}:
                continue
            mapping[stream.index] = output.add_stream_from_template(stream)
        if not any(stream.type == "video" for stream in source.streams):
            raise RuntimeError("live stream has no video track")

        deadline = time.monotonic() + max(1, duration_seconds)
        packet_count = 0
        for packet in source.demux():
            if time.monotonic() >= deadline:
                break
            target = mapping.get(packet.stream.index)
            if target is None or packet.dts is None:
                continue
            packet.stream = target
            output.mux(packet)
            packet_count += 1
        if packet_count == 0:
            raise RuntimeError("live stream produced no media packets")
    finally:
        output.close()
        source.close()


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: python -m services.recorder.capture <url> <output> <duration>")
    capture(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]))


if __name__ == "__main__":
    main()
