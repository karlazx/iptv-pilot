"""Compare ASR backends on bounded audio; never print transcript text."""

import argparse
import json
import resource
import time

from iptv_pilot.asr.audio import audio_windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio")
    parser.add_argument("--backend", choices=["faster-whisper", "openvino"], required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--language", default="zh")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be between 1 and 300")
    windows = audio_windows(args.audio, args.seconds)
    _, audio = next(windows)
    windows.close()
    start = time.perf_counter()
    if args.backend == "openvino":
        import openvino_genai

        model = openvino_genai.WhisperPipeline(args.model, args.device)
        config = model.get_generation_config()
        config.language = f"<|{args.language}|>"
        config.task = "transcribe"
        config.return_timestamps = True

        def infer():
            return model.generate(audio, config).texts[0]
    else:
        from faster_whisper import WhisperModel

        model = WhisperModel(args.model, device=args.device, compute_type="int8",
                             cpu_threads=2, num_workers=1)

        def infer():
            segments, _ = model.transcribe(audio, language=args.language,
                                            vad_filter=True, beam_size=1)
            return " ".join(segment.text for segment in segments)
    load_seconds = time.perf_counter() - start
    for iteration in range(2):
        start = time.perf_counter()
        text = infer()
        elapsed = time.perf_counter() - start
        print(json.dumps({"backend": args.backend, "device": args.device, "run": iteration,
                          "audio_seconds": len(audio) / 16000, "load_seconds": load_seconds,
                          "inference_seconds": elapsed, "realtime_factor": elapsed/(len(audio)/16000),
                          "characters": len(text),
                          "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024}),
              flush=True)


if __name__ == "__main__":
    main()
