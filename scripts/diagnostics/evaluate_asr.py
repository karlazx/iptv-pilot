"""Evaluate a local labeled audio manifest without printing transcript text.

Manifest: JSONL with {"id": "sample", "audio": "/path/sample.wav", "reference": "..."}.
Output contains recognition text and must remain private unless licensed for sharing.
"""

import argparse
import json
import os
import resource
import time
import unicodedata
from pathlib import Path

from iptv_pilot.asr.audio import audio_windows
from iptv_pilot.asr.inference import get_session


def normalize(text, converter=None):
    text = unicodedata.normalize("NFKC", text).lower()
    if converter:
        text = converter.convert(text)
    return "".join(c for c in text if c.isalnum())


def edit_distance(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for i, a in enumerate(reference, 1):
        current = [i]
        for j, b in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", choices=["cpu", "gpu", "auto", "cuda"], default="cpu")
    parser.add_argument("--openvino-model")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--simplified", action="store_true",
                        help="Normalize traditional Chinese using optional opencc-python-reimplemented")
    parser.add_argument("--openvino-cpu-control", action="store_true",
                        help="Diagnostic control: same OpenVINO engine/model on CPU")
    args = parser.parse_args()
    os.environ.update(ASR_BACKEND="auto", ASR_LANGUAGE=args.language,
                      ASR_DEVICE="cpu" if args.openvino_cpu_control else args.device)
    if args.openvino_model:
        os.environ["ASR_OPENVINO_MODEL"] = args.openvino_model
    converter = None
    if args.simplified:
        from opencc import OpenCC

        converter = OpenCC("t2s")
    start = time.monotonic()
    session = get_session(args.model)
    if args.openvino_cpu_control:
        import openvino_genai

        session._release()
        session.model = openvino_genai.WhisperPipeline(args.openvino_model, "CPU")
        session.engine, session.device = "openvino", "CPU"
    load_seconds = time.monotonic() - start
    records = []
    totals = {"errors": 0, "reference_characters": 0, "audio_seconds": 0.0,
              "inference_seconds": 0.0, "cpu_seconds": 0.0, "timestamp_errors": 0}
    # Exclusive creation avoids accidentally overwriting prior validation evidence.
    with args.output.open("x", encoding="utf-8") as output:
        for line in args.manifest.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            segments = []
            duration = 0
            before = resource.getrusage(resource.RUSAGE_SELF)
            start = time.monotonic()
            for offset, audio in audio_windows(row["audio"], 300):
                segments.extend({**s, "start": s["start"] + offset, "end": s["end"] + offset}
                                for s in session.transcribe(audio))
                duration = offset + len(audio) / 16000
            elapsed = time.monotonic() - start
            after = resource.getrusage(resource.RUSAGE_SELF)
            cpu = after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime
            text = " ".join(s["text"] for s in segments)
            ref, hyp = normalize(row["reference"], converter), normalize(text, converter)
            errors = edit_distance(ref, hyp)
            bad_timestamps = sum(not 0 <= s["start"] <= s["end"] <= duration + 0.1
                                 for s in segments)
            result = {"id": row["id"], "reference": row["reference"], "hypothesis": text,
                      "segments": segments, "errors": errors, "reference_characters": len(ref),
                      "audio_seconds": duration, "inference_seconds": elapsed,
                      "cpu_seconds": cpu, "timestamp_errors": bad_timestamps,
                      "runtime": session.metadata()}
            output.write(json.dumps(result, ensure_ascii=False) + "\n")
            output.flush()
            records.append(result)
            for key in totals:
                totals[key] += result[key]
            print(json.dumps({"sample": row["id"], "done": len(records),
                              "errors": errors, "characters": len(ref)}), flush=True)
    summary = {**totals, "samples": len(records), "runtime": session.metadata(),
               "cer": totals["errors"] / max(1, totals["reference_characters"]),
               "load_seconds": load_seconds,
               "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}
    args.output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
