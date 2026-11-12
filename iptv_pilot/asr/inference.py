"""Whisper device selection with a CPU fallback and bounded model ownership."""

from __future__ import annotations

import gc
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)
_session = None
_session_key = None


class GpuUnavailable(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def restore_chunks(chunks, mapping, duration: float) -> list[dict]:
    segments = []
    for chunk in chunks:
        start = max(0.0, min(float(chunk.start_ts), duration))
        end = duration if chunk.end_ts < 0 else min(float(chunk.end_ts), duration)
        # A segment may span two speech intervals separated by removed silence.
        # Map each endpoint independently, just like faster-whisper's segments.
        if hasattr(chunk, "word"):
            index = mapping.get_chunk_index((start+max(start, end))/2)
            segments.append({"start": mapping.get_original_time(start, index),
                             "end": mapping.get_original_time(max(start, end), index),
                             "text": chunk.word, "word": True})
        else:
            segments.append({"start": mapping.get_original_time(start),
                             "end": mapping.get_original_time(max(start, end)),
                             "text": chunk.text.strip()})
    return segments


def configuration(model: str) -> tuple:
    return (model, *(os.getenv(name, default) for name, default in (
        ("ASR_BACKEND", "auto"), ("ASR_DEVICE", "auto"),
        ("ASR_COMPUTE_TYPE", "int8"), ("ASR_CPU_THREADS", "4"),
        ("ASR_OPENVINO_MODEL", ""), ("ASR_LANGUAGE", ""),
        ("ASR_GPU_COMPUTE_TYPE", "float16"),
        ("LIVE_ASR_ENABLED", "0"),
    )))


class Session:
    def __init__(self, model: str):
        self.model_name = model
        self.model = None
        self.engine = "faster_whisper"
        self.device = "cpu"
        self.fallback_reason = None
        self.words = False
        self.word_capability = os.getenv("LIVE_ASR_ENABLED", "0") == "1"
        backend = os.getenv("ASR_BACKEND", "auto")
        device = os.getenv("ASR_DEVICE", "auto").lower()
        if backend not in {"auto", "faster_whisper", "openvino"}:
            raise ValueError(f"unsupported ASR_BACKEND={backend}")
        if device not in {"auto", "cpu", "gpu", "cuda"}:
            raise ValueError(f"unsupported ASR_DEVICE={device}")
        if device != "cpu":
            # Optional imports/probes can fail on hosts without GPU runtimes.
            try:
                if backend != "openvino" and self._cuda_available():
                    self._load_faster("cuda")
                    return
                if backend != "faster_whisper" and device != "cuda":
                    self._load_openvino()
                    return
                raise GpuUnavailable("compatible_gpu_unavailable")
            except Exception as exc:
                self.fallback_reason = getattr(exc, "reason", type(exc).__name__)
                logger.warning("ASR GPU unavailable (%s); selecting CPU", self.fallback_reason)
                self._release()
        self._load_faster("cpu")

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import ctranslate2

            return ctranslate2.get_cuda_device_count() > 0
        except Exception:
            return False

    def _load_faster(self, device: str) -> None:
        from faster_whisper import WhisperModel

        compute = os.getenv("ASR_GPU_COMPUTE_TYPE", "float16") if device == "cuda" else "int8"
        if device == "cpu":
            compute = os.getenv("ASR_COMPUTE_TYPE", "int8")
            # A GPU-only precision must not break the fallback path.
            if compute in {"float16", "int8_float16"}:
                compute = "int8"
        self.model = WhisperModel(
            self.model_name, device=device, compute_type=compute, num_workers=1,
            cpu_threads=max(1, int(os.getenv("ASR_CPU_THREADS", "4"))),
        )
        self.engine, self.device = "faster_whisper", device

    def _load_openvino(self) -> None:
        import openvino
        import openvino_genai

        model = Path(os.getenv("ASR_OPENVINO_MODEL", ""))
        # Never silently substitute a different Whisper size or download on probe.
        if not os.getenv("ASR_OPENVINO_MODEL") or not model.is_dir():
            raise GpuUnavailable("openvino_model_unavailable")
        import json

        config = json.loads((model / "config.json").read_text())
        sizes = {"tiny": 4, "base": 6, "small": 12, "medium": 24, "large": 32}
        size = self.model_name.rsplit("/", 1)[-1].replace("whisper-", "").split(".")[0]
        expected_layers = sizes.get("large" if size.startswith("large-") else size)
        if expected_layers is None or config.get("encoder_layers") != expected_layers:
            raise GpuUnavailable("openvino_model_size_mismatch")
        core = openvino.Core()
        devices = [d for d in core.available_devices if d.split(".")[0] == "GPU"]
        if not devices:
            raise GpuUnavailable("openvino_gpu_unavailable")
        # Intel's runtime validates support, driver availability and model compilation.
        properties = {"CACHE_DIR": str(model / "compiled-cache")}
        if self.word_capability:
            properties["word_timestamps"] = True
        self.model = openvino_genai.WhisperPipeline(str(model), devices[0], **properties)
        self.engine, self.device = "openvino", devices[0]

    def _release(self) -> None:
        self.model = None
        gc.collect()

    def metadata(self) -> dict:
        return {"engine": self.engine, "device": self.device,
                "model": self.model_name, "fallback_reason": self.fallback_reason}

    def transcribe(self, audio, words=False) -> list[dict]:
        self.words = words
        try:
            return self._infer(audio)
        except Exception as exc:
            if self.device == "cpu":
                raise
            self.fallback_reason = f"inference:{type(exc).__name__}"
            logger.warning("ASR GPU inference failed (%s); retrying window on CPU",
                           self.fallback_reason)
            self._release()
            self._load_faster("cpu")
            return self._infer(audio)

    def _infer(self, audio) -> list[dict]:
        language = os.getenv("ASR_LANGUAGE", "").strip() or None
        if self.engine == "faster_whisper":
            segments, _ = self.model.transcribe(
                audio, language=language, vad_filter=True, beam_size=1, word_timestamps=self.words,
            )
            if self.words:
                return [{"start": float(w.start), "end": float(w.end), "text": w.word, "word": True}
                        for s in segments for w in (s.words or [])]
            return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                    for s in segments]
        # Use the same Silero VAD as faster-whisper and restore the original timeline.
        import numpy as np
        from faster_whisper.vad import SpeechTimestampsMap, VadOptions, get_speech_timestamps

        speech = get_speech_timestamps(audio, VadOptions())
        if not speech:
            return []
        compact = np.concatenate([audio[s["start"]:s["end"]] for s in speech])
        mapping = SpeechTimestampsMap(speech, 16000)
        config = self.model.get_generation_config()
        if language:
            config.language = f"<|{language}|>"
        config.task, config.return_timestamps, config.num_beams = "transcribe", True, 1
        config.word_timestamps = self.words
        result = self.model.generate(compact, config)
        duration = len(compact) / 16000
        segments = restore_chunks((result.words or []) if self.words else result.chunks, mapping, duration)
        if not segments and any(t.strip() for t in result.texts):
            raise RuntimeError("GPU transcript has no segment timestamps")
        return segments


def get_session(model: str) -> Session:
    global _session, _session_key
    key = configuration(model)
    if _session is None or key != _session_key:
        _session = None
        gc.collect()
        _session = Session(model)
        _session_key = key
        logger.info("ASR selected engine=%s device=%s", _session.engine, _session.device)
    return _session


def runtime_info() -> dict:
    return _session.metadata() if _session is not None else {}
