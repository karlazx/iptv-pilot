from __future__ import annotations

import os
from dataclasses import dataclass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str = os.getenv(
        "MONGODB_URI",
        "mongodb://iptv_user:change-me-iptv@mongodb:27017/iptv_manager"
        "?authSource=iptv_manager&replicaSet=rs0",
    )
    mongodb_database: str = os.getenv("MONGODB_DATABASE", "iptv_manager")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()

