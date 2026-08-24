#!/usr/bin/env python3
"""Configure AI and media dependencies without printing or replacing secrets."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

DEFAULTS = {
    "INSTALL_AI_DEPS": "1",
    "INSTALL_FFMPEG": "1",
    "INSTALL_EMBEDDING_DEPS": "0",
    "PYPI_INDEX_URL": "https://pypi.org/simple",
    "FFMPEG_BIN": "auto",
    "ASR_BACKEND": "faster_whisper",
    "ASR_MODEL": "small",
    "ASR_COMPUTE_TYPE": "int8",
    "ASR_DEVICE": "cpu",
    "ASR_MAX_RETRIES": "2",
    "ASR_STALE_SECONDS": "600",
    "MODEL_NO_PROXY": "localhost,127.0.0.1,mongodb,mongot",
    "EMBEDDING_BACKEND": "none",
    "EMBEDDING_DIMENSIONS": "1024",
    "LLM_MAX_RETRIES": "2",
    "LLM_TIMEOUT_SECONDS": "90",
    "LLM_STALE_SECONDS": "300",
    "RECORDING_STALE_SECONDS": "600",
    "PUSH_PROVIDER": "coze_query",
    "PUSH_MAX_RETRIES": "3",
}


def update_env(path: Path, updates: dict[str, str]) -> None:
    if not path.is_file():
        raise SystemExit(f"environment file does not exist: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    remaining = dict(updates)
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    payload = "\n".join(output) + "\n"

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--web-port", type=int, default=5190)
    args = parser.parse_args()
    if not 1024 <= args.web_port <= 65535:
        raise SystemExit("web port must be between 1024 and 65535")
    updates = dict(DEFAULTS)
    updates["WEB_PORT"] = str(args.web_port)
    updates["WEB_CORS_ORIGINS"] = f"http://localhost:{args.web_port}"
    update_env(Path(args.env_file).resolve(), updates)
    print("AI settings updated; existing secrets preserved; mode=0600")


if __name__ == "__main__":
    main()
