"""Isolated PyAV fallback for MPEG-TS to browser-compatible MP4."""

from __future__ import annotations

import sys
from pathlib import Path

import av
from av.audio.resampler import AudioResampler


def remux(source_path: Path, output_path: Path) -> None:
    source = av.open(str(source_path), mode="r")
    output = av.open(str(output_path), mode="w", options={"movflags": "+faststart"})
    try:
        video_input = next((stream for stream in source.streams if stream.type == "video"), None)
        if video_input is None:
            raise RuntimeError("recording has no video stream")
        video_output = output.add_stream_from_template(video_input)

        audio_input = next((stream for stream in source.streams if stream.type == "audio"), None)
        audio_output = None
        resampler = None
        if audio_input is not None:
            rate = audio_input.codec_context.sample_rate or 48000
            layout = audio_input.codec_context.layout.name or "stereo"
            audio_output = output.add_stream("aac", rate=rate)
            audio_output.bit_rate = 128_000
            audio_output.layout = layout
            resampler = AudioResampler(format="fltp", layout=layout, rate=rate)

        for packet in source.demux():
            if packet.stream.index == video_input.index:
                if packet.dts is None:
                    continue
                packet.stream = video_output
                output.mux(packet)
                continue
            if audio_input is None or audio_output is None or resampler is None:
                continue
            if packet.stream.index != audio_input.index:
                continue
            for frame in packet.decode():
                for converted in resampler.resample(frame):
                    for encoded in audio_output.encode(converted):
                        output.mux(encoded)

        if audio_output is not None and resampler is not None:
            for converted in resampler.resample(None):
                for encoded in audio_output.encode(converted):
                    output.mux(encoded)
            for encoded in audio_output.encode(None):
                output.mux(encoded)
    finally:
        output.close()
        source.close()


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m services.web.remux <input> <output>")
    remux(Path(sys.argv[1]), Path(sys.argv[2]))


if __name__ == "__main__":
    main()
