from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

from pymongo import ReturnDocument
from tenacity import retry, stop_after_attempt, wait_exponential

from iptv_pilot.common.db import configure_logging, connect_database, run_forever
from iptv_pilot.push.providers import create_providers
from iptv_pilot.push.settings import load_config

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
    from iptv_pilot.common.runtime import is_draining

    if is_draining(database, "push"):
        return
    now = datetime.now(UTC)
    summary = database.summaries.find_one_and_update(
        {
            "push_processed": {"$ne": True},
            "ready_for_push": {"$ne": False},
            "push_enabled": {"$ne": False},
            "$and": [
                {"$or": [{"next_push_attempt_at": None}, {"next_push_attempt_at": {"$lte": now}}]},
                {"$or": [{"push_lease_until": None}, {"push_lease_until": {"$lte": now}}]},
            ],
        },
        {"$set": {"push_claim": uuid.uuid4().hex, "push_lease_until": now+timedelta(minutes=10)}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not summary:
        return
    owner = {"_id": summary["_id"], "push_claim": summary["push_claim"],
             "generation": summary.get("generation")}
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(20):
            try:
                database.summaries.update_one(owner, {"$set": {
                    "push_lease_until": datetime.now(UTC)+timedelta(minutes=10)}})
            except Exception:
                logger.warning("push heartbeat deferred")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        config = load_config(database)
        channels = config["channels"]
        if "coze_query" in channels:
            record_event(database, summary["_id"], "coze_query", "ready")
        delivered: list[str] = []
        failures: list[str] = []
        providers = create_providers(channels, config)
        email_mode = os.getenv("PUSH_EMAIL_MODE", "fallback").lower()
        coze_succeeded = False
        for provider in providers:
            if not database.summaries.find_one(owner, {"_id": 1}):
                return
            if database.push_events.find_one({"summary_id": summary["_id"],
                                               "provider": provider.name, "status": "delivered"}):
                delivered.append(provider.name)
                if provider.name == "coze":
                    coze_succeeded = True
                continue
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
        if failures or (providers and not delivered):
            raise RuntimeError("; ".join(failures) or "no push provider delivered")
        database.summaries.update_one(
            owner,
            {
                "$set": {
                    "push_processed": True,
                    "pushed": bool(delivered),
                    "pushed_at": datetime.now(UTC) if delivered else None,
                    "push_channels": channels,
                    "push_error": "; ".join(failures)[:2000] if failures else None,
                    "next_push_attempt_at": None,
                    "push_lease_until": None,
                }
            },
        )
    except Exception as exc:
        retry_count = int(summary.get("push_retry_count", 0)) + 1
        maximum = int(os.getenv("PUSH_MAX_RETRIES", "3"))
        update = {
            "push_lease_until": None,
            "push_retry_count": retry_count,
            "push_error": str(exc)[:2000],
            "push_processed": retry_count > maximum,
        }
        if retry_count <= maximum:
            update["next_push_attempt_at"] = datetime.now(UTC) + timedelta(seconds=60)
        database.summaries.update_one(
            owner, {"$set": update}
        )
        logger.exception("push failed summary_id=%s retry=%s/%s", summary["_id"], retry_count, maximum)
    finally:
        stopped.set()
        thread.join(timeout=2)


def main() -> None:
    configure_logging()
    database = connect_database()
    run_forever(lambda: run_once(database), float(os.getenv("PUSH_POLL_SECONDS", "10")))


if __name__ == "__main__":
    main()
