#!/usr/bin/env python3
"""Prepare rc.6 data: source grouping metadata, encode index, push defaults.

Idempotent.  Run once after upgrading:

    python -m scripts.migrate_rc6 --dry-run
    python -m scripts.migrate_rc6 --apply
"""

from __future__ import annotations

import argparse
import logging

from iptv_pilot.common.db import configure_logging, connect_database

LOG = logging.getLogger("migrate_rc6")

PROVIDER_BY_ORIGIN = {
    "seed": "IPTV-API 同步",
    "import": "M3U 导入",
}


def backfill_providers(database, apply: bool) -> int:
    """Give legacy rows a stable provider so the two-level source page groups well."""
    updated = 0
    for row in database.sources.find({"$or": [{"provider": ""}, {"provider": {"$exists": False}}]}):
        url = row.get("url") or ""
        origin = row.get("origin") or "custom"
        if ":4023/" in url or ":4022/" in url or "/rtp/" in url:
            provider = "官方组播"
        else:
            provider = PROVIDER_BY_ORIGIN.get(origin, "第三方" if origin == "migration" else "用户自定义")
        LOG.info("source=%s origin=%s provider=%s", row.get("source_id"), origin, provider)
        if apply:
            database.sources.update_one(
                {"source_id": row["source_id"]}, {"$set": {"provider": provider}}
            )
        updated += 1
    return updated


def ensure_indexes(database, apply: bool) -> None:
    existing = {row["name"] for row in database.recordings.list_indexes()}
    wanted = [
        ([("encode_status", 1), ("next_encode_attempt_at", 1)], {"name": "recording_encode_queue"}),
        ([("encode_claim_id", 1)], {"name": "recording_encode_claim", "sparse": True}),
    ]
    for keys, options in wanted:
        if options["name"] in existing:
            continue
        LOG.info("create index %s", options["name"])
        if apply:
            database.recordings.create_index(keys, **options)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="only report (default)")
    args = parser.parse_args()
    apply = args.apply and not args.dry_run
    configure_logging()
    database = connect_database()
    LOG.info("mode=%s", "apply" if apply else "dry-run")
    providers = backfill_providers(database, apply)
    LOG.info("sources missing provider=%s", providers)
    ensure_indexes(database, apply)
    LOG.info("done")


if __name__ == "__main__":
    main()
