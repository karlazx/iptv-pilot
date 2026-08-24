from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from pymongo import MongoClient
from pymongo.database import Database

from iptv_pilot.common.config import settings

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def connect_database(retries: int = 60, delay: float = 2.0) -> Database[Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client: MongoClient[Any] = MongoClient(
                settings.mongodb_uri,
                serverSelectionTimeoutMS=5000,
                tz_aware=True,
            )
            client.admin.command("ping")
            logger.info("connected to MongoDB database=%s", settings.mongodb_database)
            return client[settings.mongodb_database]
        except Exception as exc:  # startup retry boundary
            last_error = exc
            logger.warning("MongoDB unavailable attempt=%s/%s error=%s", attempt, retries, exc)
            time.sleep(delay)
    raise RuntimeError("could not connect to MongoDB") from last_error


def run_forever(job: Callable[[], None], interval_seconds: float) -> None:
    while True:
        try:
            job()
        except Exception:
            logger.exception("worker iteration failed")
        time.sleep(interval_seconds)

