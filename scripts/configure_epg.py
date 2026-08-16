#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
from pathlib import Path


EPG_URLS = "/data/m3u/epg/epg.xml"


def main() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        raise SystemExit(f"missing env file: {env_file}")

    lines = env_file.read_text(encoding="utf-8").splitlines()
    replacement = f"EPG_URLS={EPG_URLS}"
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("EPG_URLS="):
            updated.append(replacement)
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)

    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=env_file.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write("\n".join(updated) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, env_file)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)

    print("EPG configured: iptv-api mapped XML first, external sources as fallback")


if __name__ == "__main__":
    main()
