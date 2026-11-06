#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path

SAFE_VALUE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_source(path: Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if ":" not in raw_line:
            continue
        label, value = raw_line.split(":", 1)
        values[label.strip().lower()] = value.strip()

    model = values.get("model_id", "")
    api_key = values.get("api_key", "")
    if not model or not SAFE_VALUE.fullmatch(model):
        raise ValueError("model_id is missing or invalid")
    if len(api_key) < 20 or not SAFE_VALUE.fullmatch(api_key):
        raise ValueError("api_key is missing or invalid")
    return model, api_key


def update_env(path: Path, replacements: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    pending = dict(replacements)
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in pending:
            output.append(f"{key}={pending.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in pending.items())

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write("\n".join(output) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure DeepSeek without printing its API key")
    parser.add_argument("source", type=Path)
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()

    model, api_key = parse_source(args.source)
    update_env(
        args.env_file,
        {
            "LLM_PROVIDER": "custom",
            "LLM_MODEL": model,
            "LLM_API_KEY": api_key,
            "LLM_BASE_URL": "https://api.deepseek.com",
        },
    )
    print(f"DeepSeek configured: provider=custom model={model} base_url=https://api.deepseek.com")


if __name__ == "__main__":
    main()
