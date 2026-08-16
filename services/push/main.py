from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from tenacity import retry, stop_after_attempt, wait_exponential

from services.common.db import configure_logging, connect_database, run_forever
from services.push.providers import configured_channels, create_providers

logger = logging.getLogger(__name__)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=5, min=5, max=120), reraise=True)
def deliver(provider, summary: dict) -> bool:
    return provider.push_summary(summary)


def record_event(database, summary_id, provider: str, status: str, error: str | None = None) -> None:
    database.push_events.update_one(
        {"summary_id": summary_id, "provider": provider},
        {
            "$set": {
                "status": status,
                "error": error,
                "updated_at": datetime.now(UTC),
            },
            "$setOnInsert": {"created_at": datetime.now(UTC)},
        },
        upsert=True,
    )


def run_once(database) -> None:
    now = datetime.now(UTC)
    summary = database.summaries.find_one(
        {
            "push_processed": {"$ne": True},
            "push_enabled": {"$ne": False},
            "$or": [
                {"next_push_attempt_at": {"$exists": False}},
                {"next_push_attempt_at": {"$lte": now}},
            ],
        },
        sort=[("created_at", 1)],
    )
    if not summary:
        return
    try:
        channels = configured_channels()
        if "coze_query" in channels:
            record_event(database, summary["_id"], "coze_query", "ready")
        delivered: list[str] = []
        failures: list[str] = []
        providers = create_providers()
        email_mode = os.getenv("PUSH_EMAIL_MODE", "fallback").lower()
        coze_succeeded = False
        for provider in providers:
            if provider.name == "email" and email_mode == "fallback" and coze_succeeded:
                record_event(database, summary["_id"], "email", "skipped")
                continue
            try:
                if deliver(provider, summary):
                    delivered.append(provider.name)
                    record_event(database, summary["_id"], provider.name, "delivered")
                    if provider.name == "coze":
                        coze_succeeded = True
            except Exception as provider_error:
                failures.append(f"{provider.name}: {provider_error}")
                record_event(
                    database,
                    summary["_id"],
                    provider.name,
                    "failed",
                    str(provider_error)[:2000],
                )
                logger.exception("push provider failed provider=%s", provider.name)
        if providers and not delivered:
            raise RuntimeError("; ".join(failures) or "no push provider delivered")
        database.summaries.update_one(
            {"_id": summary["_id"]},
            {
                "$set": {
                    "push_processed": True,
                    "pushed": bool(delivered),
                    "pushed_at": datetime.now(UTC) if delivered else None,
                    "push_channels": channels,
                    "push_error": "; ".join(failures)[:2000] if failures else None,
                    "next_push_attempt_at": None,
                }
            },
        )
    except Exception as exc:
        retry_count = int(summary.get("push_retry_count", 0)) + 1
        maximum = int(os.getenv("PUSH_MAX_RETRIES", "3"))
        for channel in configured_channels():
            if channel != "coze_query":
                record_event(database, summary["_id"], channel, "failed", str(exc)[:2000])
        update = {
            "push_retry_count": retry_count,
            "push_error": str(exc)[:2000],
            "push_processed": retry_count > maximum,
        }
        if retry_count <= maximum:
            update["next_push_attempt_at"] = datetime.now(UTC) + timedelta(seconds=60)
        database.summaries.update_one(
            {"_id": summary["_id"]}, {"$set": update}
        )
        logger.exception("push failed summary_id=%s retry=%s/%s", summary["_id"], retry_count, maximum)


def main() -> None:
    configure_logging()
    database = connect_database()
    run_forever(lambda: run_once(database), float(os.getenv("PUSH_POLL_SECONDS", "10")))


if __name__ == "__main__":
    main()
