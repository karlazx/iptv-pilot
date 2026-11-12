"""Decode bounded mono audio windows instead of materializing a whole recording."""

from collections.abc import Iterator


def audio_windows(path: str, seconds: int = 300, sample_rate: int = 16000) -> Iterator:
    import av
    import numpy as np

    maximum = seconds * sample_rate
    pending = []
    count = 0
    offset = 0
    with av.open(path, mode="r") as source:
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        for packet in source.demux(audio=0):
            try:
                frames = packet.decode()
            except av.error.InvalidDataError:
                continue
            for frame in frames:
                for converted in resampler.resample(frame):
                    samples = converted.to_ndarray().reshape(-1)
                    while samples.size:
                        size = min(maximum - count, samples.size)
                        pending.append(samples[:size].copy())
                        samples = samples[size:]
                        count += size
                        if count == maximum:
                            yield offset / sample_rate, np.concatenate(pending)
                            offset += count
                            pending, count = [], 0
        for converted in resampler.resample(None):
            samples = converted.to_ndarray().reshape(-1)
            while samples.size:
                size = min(maximum - count, samples.size)
                pending.append(samples[:size].copy())
                samples = samples[size:]
                count += size
                if count == maximum:
                    yield offset / sample_rate, np.concatenate(pending)
                    offset += count
                    pending, count = [], 0
        if count:
            yield offset / sample_rate, np.concatenate(pending)
