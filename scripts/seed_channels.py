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
                active_ids = list(rows_by_id)
                operations = []
                for rows in rows_by_id.values():
                    selected = rows[0]
                    candidates = list(dict.fromkeys(row["m3u_url"] for row in rows))
                    existing = database.channels.find_one(
                        {"channel_id": selected["channel_id"]}, {"m3u_url": 1}
                    )
                    if existing and existing.get("m3u_url") in candidates:
                        selected = next(
                            row for row in rows if row["m3u_url"] == existing["m3u_url"]
                        )
                    operations.append(
                        UpdateOne(
                            {"channel_id": selected["channel_id"]},
                            {"$set": {**selected, "stream_candidates": candidates}},
                            upsert=True,
                        )
                    )
                if operations:
                    result = database.channels.bulk_write(operations, ordered=False)
                    disabled = database.channels.update_many(
                        {"source": "iptv-api", "channel_id": {"$nin": active_ids}},
                        {"$set": {"enabled": False, "updated_at": datetime.now(UTC)}},
                    )
                    logging.info(
                        "channel sync active=%s matched=%s upserted=%s disabled=%s",
                        len(active_ids),
                        result.matched_count,
                        result.upserted_count,
                        disabled.modified_count,
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
