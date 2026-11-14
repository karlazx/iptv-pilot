"""Environment isolation for imageio's precompiled FFmpeg executable."""

import os


def ffmpeg_environment(executable):
    environment = dict(os.environ)
    if "imageio_ffmpeg" in str(executable):
        # Do not mix bundled glibc code with the system's gconv modules.
        # Explicit operator configuration and system FFmpeg stay unchanged.
        environment.setdefault("GCONV_PATH", "/nonexistent")
    return environment
