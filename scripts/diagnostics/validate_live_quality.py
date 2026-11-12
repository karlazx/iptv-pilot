"""Compare verified live windows with offline ASR on a referenced WAV corpus.

The manifest is JSONL with `audio` and `reference`; relative paths use its directory.
Only aggregate quality metrics are printed, never reference or hypothesis text.
"""

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
from opencc import OpenCC

from iptv_pilot.asr.audio import audio_windows
from iptv_pilot.asr.inference import get_session
from iptv_pilot.recorder.live_audio import VerifiedSegmenter
from scripts.diagnostics.evaluate_asr import edit_distance, normalize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--max-cer-increase", type=float, default=.02)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    audio = []
    for row in rows:
        path = args.manifest.parent / row["audio"]
        audio.extend(samples for _, samples in audio_windows(str(path), 300))
        audio.append(np.zeros(16000, dtype=np.float32))
    samples = np.concatenate(audio)
    reference = normalize("".join(r["reference"] for r in rows), OpenCC("t2s"))
    if not reference:
        parser.error("nonempty reference text required")
    os.environ["LIVE_ASR_ENABLED"] = "1"
    session = get_session(args.model)
    results = {}
    for name in ("offline", "verified_live"):
        started = time.monotonic()
        text = []
        if name == "offline":
            windows = (samples[i:i+300*16000] for i in range(0, len(samples), 300*16000))
        else:
            splitter = VerifiedSegmenter(300)
            pcm = (np.clip(samples, -1, .999969)*32768).astype("<i2").tobytes()
            parts = []
            for i in range(0, len(pcm), 32000):
                parts.extend(splitter.feed(pcm[i:i+32000]))
            parts.extend(splitter.feed(b"", final=True))
            windows = (np.frombuffer(p["pcm"], dtype="<i2").astype(np.float32)/32768 for p in parts)
        for window in windows:
            text.extend(s["text"] for s in session.transcribe(window))
        hypothesis = normalize(" ".join(text), OpenCC("t2s"))
        errors = edit_distance(reference, hypothesis)
        results[name] = {"errors": errors, "cer": errors/len(reference),
                         "seconds": time.monotonic()-started}
    results.update(reference_characters=len(reference), audio_seconds=len(samples)/16000,
                   runtime=session.metadata())
    args.output.write_text(json.dumps(results, indent=2))
    print(json.dumps(results))
    assert results["verified_live"]["cer"]-results["offline"]["cer"] <= args.max_cer_increase


if __name__ == "__main__":
    main()
