from types import SimpleNamespace

import pytest

from iptv_pilot.asr import inference


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    for name in ("ASR_BACKEND", "ASR_DEVICE", "ASR_COMPUTE_TYPE", "ASR_OPENVINO_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(inference, "_session", None)
    monkeypatch.setattr(inference, "_session_key", None)


def fake_cpu(monkeypatch):
    calls = []

    def load(self, device):
        calls.append(device)
        self.engine, self.device = "faster_whisper", device
        self.model = object()

    monkeypatch.setattr(inference.Session, "_load_faster", load)
    return calls


def test_explicit_cpu_never_probes_gpu(monkeypatch):
    monkeypatch.setenv("ASR_DEVICE", "cpu")
    calls = fake_cpu(monkeypatch)
    monkeypatch.setattr(inference.Session, "_cuda_available", lambda self: pytest.fail("GPU probe"))
    assert inference.get_session("small").metadata()["device"] == "cpu"
    assert calls == ["cpu"]


@pytest.mark.parametrize("failure", [ImportError, RuntimeError, OSError])
def test_missing_runtime_driver_or_model_falls_back(monkeypatch, failure):
    calls = fake_cpu(monkeypatch)
    monkeypatch.setattr(inference.Session, "_cuda_available", lambda self: False)

    def unavailable(self):
        raise failure("unavailable")

    monkeypatch.setattr(inference.Session, "_load_openvino", unavailable)
    session = inference.get_session("small")
    assert calls == ["cpu"]
    assert session.metadata()["fallback_reason"] == failure.__name__
    assert inference.get_session("small") is session  # no retry storm on every window


def test_inference_failure_retries_same_audio_on_cpu(monkeypatch):
    calls = fake_cpu(monkeypatch)
    monkeypatch.setattr(inference.Session, "_cuda_available", lambda self: True)
    session = inference.get_session("small")
    audio = object()
    received = []

    def infer(self, value):
        received.append((self.device, value))
        if self.device == "cuda":
            raise RuntimeError("device lost during generator consumption")
        return [{"start": 1, "end": 2, "text": "recovered"}]

    monkeypatch.setattr(inference.Session, "_infer", infer)
    assert session.transcribe(audio)[0]["text"] == "recovered"
    assert received == [("cuda", audio), ("cpu", audio)]
    assert calls == ["cuda", "cpu"]
    assert session.metadata()["fallback_reason"] == "inference:RuntimeError"


def test_gpu_failure_then_cpu_failure_is_reported(monkeypatch):
    fake_cpu(monkeypatch)
    monkeypatch.setattr(inference.Session, "_cuda_available", lambda self: True)
    session = inference.get_session("small")

    def broken(self, value):
        raise ValueError("invalid audio")

    monkeypatch.setattr(inference.Session, "_infer", broken)
    with pytest.raises(ValueError, match="invalid audio"):
        session.transcribe(object())


def test_auto_gpu_can_select_intel_without_cuda(monkeypatch):
    fake_cpu(monkeypatch)
    monkeypatch.setenv("ASR_DEVICE", "gpu")
    monkeypatch.setattr(inference.Session, "_cuda_available", lambda self: False)

    def intel(self):
        self.engine, self.device, self.model = "openvino", "GPU", object()

    monkeypatch.setattr(inference.Session, "_load_openvino", intel)
    assert inference.get_session("small").metadata()["engine"] == "openvino"


def test_model_and_configuration_changes_invalidate_cached_session(monkeypatch):
    fake_cpu(monkeypatch)
    monkeypatch.setenv("ASR_DEVICE", "cpu")
    first = inference.get_session("small")
    monkeypatch.setenv("ASR_LANGUAGE", "zh")
    second = inference.get_session("small")
    assert first is not second
    assert inference.get_session("base") is not second


def test_failed_model_change_does_not_break_the_previous_configuration(monkeypatch):
    monkeypatch.setenv("ASR_DEVICE", "cpu")
    calls = fake_cpu(monkeypatch)
    inference.get_session("small")
    load = inference.Session._load_faster

    def missing(self, device):
        if self.model_name == "medium":
            raise RuntimeError("model not cached")
        load(self, device)

    monkeypatch.setattr(inference.Session, "_load_faster", missing)
    with pytest.raises(RuntimeError, match="not cached"):
        inference.get_session("medium")
    assert inference.get_session("small").model_name == "small"
    assert calls == ["cpu", "cpu"]


def test_cpu_fallback_does_not_inherit_gpu_only_precision(monkeypatch):
    import sys

    calls = []
    monkeypatch.setenv("ASR_DEVICE", "cpu")
    monkeypatch.setenv("ASR_COMPUTE_TYPE", "float16")
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(
        WhisperModel=lambda *args, **kwargs: calls.append(kwargs) or object(),
    ))
    inference.get_session("small")
    assert calls[0]["compute_type"] == "int8"


@pytest.mark.parametrize("device,backend", [("cpux", "auto"), ("cpu", "unknown")])
def test_configuration_typo_is_not_hidden_by_fallback(monkeypatch, device, backend):
    fake_cpu(monkeypatch)
    monkeypatch.setenv("ASR_DEVICE", device)
    monkeypatch.setenv("ASR_BACKEND", backend)
    with pytest.raises(ValueError):
        inference.get_session("small")


def test_segment_crossing_removed_silence_restores_each_endpoint():
    # Speech in [0,10] and [30,40] becomes a compact [0,20] input.
    # The segment [8,12] spans that removed 20-second gap.
    mapping = SimpleNamespace(get_original_time=lambda t: t if t < 10 else t + 20)
    chunks = [SimpleNamespace(start_ts=8, end_ts=12, text=" bridge "),
              SimpleNamespace(start_ts=17, end_ts=-1, text="tail")]
    assert inference.restore_chunks(chunks, mapping, 20) == [
        {"start": 8, "end": 32, "text": "bridge"},
        {"start": 37, "end": 40, "text": "tail"},
    ]

