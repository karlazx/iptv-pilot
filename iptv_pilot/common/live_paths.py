from __future__ import annotations

import hashlib
from pathlib import Path


def channel_cache_key(channel_id: str) -> str:
    return hashlib.sha256(channel_id.encode("utf-8")).hexdigest()[:20]


def channel_cache_directory(root: Path, channel_id: str) -> Path:
    return root / channel_cache_key(channel_id)
