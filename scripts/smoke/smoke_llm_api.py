#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a sanitized OpenAI-compatible LLM smoke test")
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()
    env = read_env(args.env_file)
    api_key = env.get("LLM_API_KEY", "")
    model = env.get("LLM_MODEL", "")
    base_url = env.get("LLM_BASE_URL", "").rstrip("/")
    if not api_key or not model or not base_url:
        raise RuntimeError("LLM_API_KEY, LLM_MODEL and LLM_BASE_URL are required")

    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Output JSON only."},
                {"role": "user", "content": "Return exactly one JSON object with ok=true."},
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"LLM API returned HTTP {error.code}") from None

    choices = payload.get("choices", [])
    content = choices[0].get("message", {}).get("content", "") if choices else ""
    parsed = json.loads(content)
    if parsed.get("ok") is not True:
        raise RuntimeError("LLM API returned an unexpected JSON object")
    print(f"LLM smoke passed: model={model} json_output=true")


if __name__ == "__main__":
    main()
