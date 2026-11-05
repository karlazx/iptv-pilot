#!/usr/bin/env python3
"""Prepare recordings for rc.5 concurrent recording.

- Drops the legacy unique ``(subscription_id, start_time)`` index that blocks
  one subscription from recording two channels at the same time.
- Creates the concurrency indexes.
- Reports duplicate ``(channel_id, start_time)`` recordings; ``--merge`` folds
  queued/failed duplicates into the most advanced row.
- With ``--purge-parts`` removes stale ``.part.ts`` fragments left by a crash.

Usage:
    python -m scripts.migrate_recorder_concurrency --dry-run
    python -m scripts.migrate_recorder_concurrency --apply
    python -m scripts.migrate_recorder_concurrency --apply --merge --purge-parts
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from iptv_pilot.common.db import configure_logging, connect_database

LOG = logging.getLogger("migrate_recorder_concurrency")
STATUS_RANK = {"completed": 4, "recording": 3, "queued": 2, "failed": 1}


def _indexes(database) -> dict[str, dict]:
    try:
        return {row["name"]: row for row in database.recordings.list_indexes()}
    except Exception:
        return {}


def ensure_indexes(database, apply: bool) -> None:
    existing = _indexes(database)
    legacy = existing.get("recording_dedupe")
    if legacy and legacy.get("unique"):
        LOG.info("drop legacy unique index recording_dedupe")
        if apply:
            database.recordings.drop_index("recording_dedupe")
    wanted = [
        ([("status", 1), ("heartbeat_at", 1)], {"name": "recording_heartbeat"}),
        ([("claim_id", 1)], {"name": "recording_claim", "sparse": True}),
        ([("channel_id", 1), ("start_time", 1)], {"name": "recording_channel_start"}),
        ([("subscription_id", 1), ("start_time", 1)], {"name": "recording_subscription_start"}),
    ]
    for keys, options in wanted:
        if options["name"] in existing:
            continue
        LOG.info("create index %s", options["name"])
        if apply:
            database.recordings.create_index(keys, **options)


def duplicate_groups(database) -> list[dict]:
    pipeline = [
        {
            "$group": {
                "_id": {"channel_id": "$channel_id", "start_time": "$start_time"},
                "count": {"$sum": 1},
                "ids": {"$push": "$_id"},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
    ]
    return list(database.recordings.aggregate(pipeline))


def merge_duplicates(database, apply: bool) -> None:
    for group in duplicate_groups(database):
        rows = list(database.recordings.find({"_id": {"$in": group["ids"]}}))
        rows.sort(
            key=lambda row: (
                STATUS_RANK.get(row.get("status"), 0),
                row.get("completed_at") or row.get("created_at") or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        keeper, extras = rows[0], rows[1:]
        LOG.info(
            "duplicate channel=%s start=%s keep=%s drop=%s",
            group["_id"].get("channel_id"),
            group["_id"].get("start_time"),
            keeper["_id"],
            [str(row["_id"]) for row in extras],
        )
        if not apply:
            continue
        subscription_ids = set(keeper.get("subscription_ids") or [])
        for row in rows:
            if row.get("subscription_id"):
                subscription_ids.add(row["subscription_id"])
            subscription_ids.update(row.get("subscription_ids") or [])
        database.recordings.update_one(
            {"_id": keeper["_id"]},
            {"$set": {"subscription_ids": sorted(subscription_ids, key=str)}},
        )
        removable = [
            row["_id"]
            for row in extras
            if row.get("status") in {"queued", "failed"} and not row.get("file_path")
        ]
        if removable:
            database.recordings.delete_many({"_id": {"$in": removable}})


def purge_partials(apply: bool) -> None:
    root = Path("/data/recordings")
    cutoff = datetime.now(UTC) - timedelta(hours=6)
    stale = [path for path in root.rglob("*.part.ts") if path.stat().st_mtime < cutoff.timestamp()]
    for path in stale:
        LOG.info("purge partial %s", path)
        if apply:
            path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="only report (default)")
    parser.add_argument("--merge", action="store_true", help="merge duplicate channel/start rows")
    parser.add_argument("--purge-parts", action="store_true", help="remove stale .part.ts files")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run
    configure_logging()
    database = connect_database()
    LOG.info("mode=%s", "apply" if apply else "dry-run")
    ensure_indexes(database, apply)
    if args.merge:
        merge_duplicates(database, apply)
    else:
        groups = duplicate_groups(database)
        LOG.info("duplicate channel/start groups=%s (use --merge to fold)", len(groups))
    if args.purge_parts:
        purge_partials(apply)
    LOG.info("done")


if __name__ == "__main__":
    main()
