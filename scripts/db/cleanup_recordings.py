from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from pymongo import MongoClient

from iptv_pilot.common.media_refs import retire_files


def main() -> None:
    client = MongoClient(os.environ["MONGODB_URI"], tz_aware=True)
    database = client[os.getenv("MONGODB_DATABASE", "iptv_pilot")]
    cutoff = datetime.now(UTC) - timedelta(days=int(os.getenv("RECORDING_RETENTION_DAYS", "30")))
    bookmarked_ids = {
        row["item_id"] for row in database.bookmarks.find({"item_type": "recording"}, {"item_id": 1})
    }
    query = {"status": {"$in": ["completed", "asr_done", "summarized"]}, "completed_at": {"$lt": cutoff}}
    removed = 0
    for recording in database.recordings.find(query):
        if recording["_id"] in bookmarked_ids:
            continue
        if recording.get("asr_enabled", True) and recording.get("status") == "completed":
            continue
        paths = [recording.get(k) for k in ["file_path", "asr_source_path", "original_file_path"]]
        paths.extend(p["path"] for p in recording.get("recording_parts", []))
        if not retire_files(database, paths):
            continue
        database.recordings.delete_one({"_id": recording["_id"]})
        removed += 1
    print(f"removed={removed}")


if __name__ == "__main__":
    main()

