from __future__ import annotations

import argparse
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from pymongo import MongoClient, UpdateOne

ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')


def parse_m3u(path: Path):
    current: dict | None = None
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(ATTRIBUTE.findall(line))
            name = line.rsplit(",", 1)[-1].strip()
            if "更新时间" in attrs.get("group-title", ""):
                current = None
                continue
            channel_id = attrs.get("tvg-id") or attrs.get("tvg-name") or name
            current = {"channel_id": channel_id, "name": name, "raw_m3u": attrs}
        elif current and line and not line.startswith("#"):
            yield {
                **current,
                "m3u_url": line,
                "enabled": True,
                "source": "iptv-api",
                "updated_at": datetime.now(UTC),
            }
            current = None


def sync_channels(database, rows_by_id: dict[str, list[dict]]) -> dict:
    """Upsert seed channels without ever overriding a user-locked source."""
    from iptv_pilot.sources import store
    from iptv_pilot.sources.models import make_source, ordered_sources, url_key

    active_ids = list(rows_by_id)
    operations = []
    created_sources = 0
    preserved_primary = 0
    touched_channels = 0

    for rows in rows_by_id.values():
        channel_id = rows[0]["channel_id"]
        urls = list(dict.fromkeys(row["m3u_url"] for row in rows))
        existing_channel = database.channels.find_one({"channel_id": channel_id})
        existing_sources = list(database.sources.find({"channel_id": channel_id}))
        locked = next(
            (
                row
                for row in ordered_sources(existing_sources)
                if row.get("locked") and row.get("enabled", True)
            ),
            None,
        )
        # Selecting an existing channel URL must never move a locked primary.
        selected = rows[0]
        if locked:
            preserved_primary += 1
        elif existing_channel and existing_channel.get("m3u_url") in urls:
            selected = next(row for row in rows if row["m3u_url"] == existing_channel["m3u_url"])

        update: dict = {
            "channel_id": channel_id,
            "name": selected["name"],
            "raw_m3u": selected["raw_m3u"],
            "enabled": True,
            "source": "iptv-api",
            "updated_at": datetime.now(UTC),
        }
        if not locked:
            update["m3u_url"] = selected["m3u_url"]
        operations.append(UpdateOne({"channel_id": channel_id}, {"$set": update}, upsert=True))

        known = {row.get("url_key") for row in existing_sources}
        for index, url in enumerate(urls):
            key = url_key(url)
            if key in known:
                database.sources.update_many(
                    {"channel_id": channel_id, "url_key": key},
                    {"$set": {"enabled": True, "missing_from_seed": False}},
                )
                continue
            document = make_source(
                channel_id=channel_id,
                url=url,
                label=f"{selected['name']} 线路 {index + 1}",
                origin="seed",
                origin_key=f"iptv-api:{channel_id}",
                kind="udpxy" if ":4022/" in url or "/rtp/" in url else "m3u",
                provider="iptv-api",
                priority=(locked.get("priority") + 1 if locked else 0) + index,
            )
            database.sources.insert_one(document)
            known.add(key)
            created_sources += 1

        database.sources.update_many(
            {
                "channel_id": channel_id,
                "origin": "seed",
                "url_key": {"$nin": [url_key(url) for url in urls]},
            },
            {"$set": {"enabled": False, "missing_from_seed": True}},
        )
        touched_channels += 1

    result = database.channels.bulk_write(operations, ordered=False) if operations else None
    if result is None:
        return {"active": 0, "upserted": 0, "disabled": 0, "sources_created": 0, "locked_preserved": 0}
    upserted = result.upserted_count

    # A channel removed from the M3U keeps working when it still owns a locked
    # or user-provided source; only fully seed-owned channels are disabled.
    removable = [
        channel["channel_id"]
        for channel in database.channels.find(
            {"source": "iptv-api", "channel_id": {"$nin": active_ids}},
            {"channel_id": 1},
        )
        if not database.sources.find_one(
            {
                "channel_id": channel["channel_id"],
                "enabled": True,
                "$or": [{"locked": True}, {"origin": {"$in": ["custom", "import"]}}],
            }
        )
    ]
    disabled = 0
    if removable:
        result = database.channels.update_many(
            {"channel_id": {"$in": removable}},
            {"$set": {"enabled": False, "updated_at": datetime.now(UTC)}},
        )
        disabled = result.modified_count

    for channel_id in rows_by_id:
        channel = database.channels.find_one({"channel_id": channel_id})
        if channel:
            store.sync_channel_mirror(database, channel)

    return {
        "active": len(active_ids),
        "upserted": upserted,
        "disabled": disabled,
        "sources_created": created_sources,
        "locked_preserved": preserved_primary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import iptv-api M3U channels into MongoDB")
    parser.add_argument("path", type=Path)
    parser.add_argument("--uri", default=None)
    parser.add_argument("--watch", action="store_true", help="keep syncing when the M3U changes")
    args = parser.parse_args()
    uri = args.uri or os.environ["MONGODB_URI"]
    database_name = os.getenv("MONGODB_DATABASE", "iptv_pilot")
    database = MongoClient(uri, tz_aware=True)[database_name]
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    last_mtime = None
    while True:
        try:
            mtime = args.path.stat().st_mtime
            if mtime != last_mtime:
                rows_by_id: dict[str, list[dict]] = {}
                for row in parse_m3u(args.path):
                    rows_by_id.setdefault(row["channel_id"], []).append(row)
                if rows_by_id:
                    summary = sync_channels(database, rows_by_id)
                    logging.info(
                        "channel sync active=%s matched=%s upserted=%s disabled=%s "
                        "sources_created=%s locked_preserved=%s",
                        summary["active"],
                        summary["active"],
                        summary["upserted"],
                        summary["disabled"],
                        summary["sources_created"],
                        summary["locked_preserved"],
                    )
                else:
                    logging.warning("no channels found in %s", args.path)
                last_mtime = mtime
        except FileNotFoundError:
            logging.warning("waiting for M3U file: %s", args.path)
        if not args.watch:
            break
        time.sleep(max(30, int(os.getenv("CHANNEL_SYNC_INTERVAL_SECONDS", "300"))))


if __name__ == "__main__":
    main()
