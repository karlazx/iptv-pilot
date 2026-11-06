from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import MongoClient


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
        path = Path(recording.get("file_path", ""))
        if path.is_file():
            path.unlink()
        database.recordings.delete_one({"_id": recording["_id"]})
        removed += 1
    print(f"removed={removed}")


if __name__ == "__main__":
    main()

