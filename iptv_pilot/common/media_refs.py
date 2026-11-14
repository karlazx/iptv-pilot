"""Cross-process reader leases for immutable recording artifacts."""

import hashlib
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo.errors import DuplicateKeyError


class MediaBusy(RuntimeError):
    pass


def key(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


class MediaReader:
    def __init__(self, database, path, ttl=120):
        self.db, self.path, self.ttl = database, Path(path), ttl
        self.identity, self.token = key(path), uuid.uuid4().hex
        self.stop = threading.Event()
        self.thread = None

    def __enter__(self):
        now = datetime.now(UTC)
        self.db.media_readers.update_one({"_id": self.identity},
                                        {"$pull": {"readers": {"until": {"$lte": now}}}})
        try:
            self.db.media_readers.update_one({"_id": self.identity, "retired": {"$ne": True}},
                {"$push": {"readers": {"owner": self.token, "until": now+timedelta(seconds=self.ttl)}},
                 "$setOnInsert": {"path": str(self.path)}}, upsert=True)
        except DuplicateKeyError as exc:
            raise MediaBusy("media artifact was retired; reload recording") from exc
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def _heartbeat(self):
        while not self.stop.wait(20):
            try:
                self.db.media_readers.update_one({"_id": self.identity, "readers.owner": self.token},
                    {"$set": {"readers.$.until": datetime.now(UTC)+timedelta(seconds=self.ttl)}})
            except Exception:
                # Failed heartbeats do not authorize deletion before lease expiry.
                pass

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.db.media_readers.update_one({"_id": self.identity}, {"$pull": {"readers": {"owner": self.token}}})

    def __exit__(self, *args):
        self.close()


def retire_file(database, path, root=None):
    """Retire before unlinking, so late readers cannot race the delete."""
    path, root = Path(path).resolve(), Path(root or os.getenv("RECORDINGS_ROOT", "/data/recordings")).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("media artifact is outside recordings root")
    now = datetime.now(UTC)
    identity = key(path)
    try:
        result = database.media_readers.update_one(
            {"_id": identity, "readers": {"$not": {"$elemMatch": {"until": {"$gt": now}}}}},
            {"$set": {"retired": True, "path": str(path), "retired_at": now}}, upsert=True)
    except DuplicateKeyError:
        return False
    if result.matched_count or result.upserted_id:
        path.unlink(missing_ok=True)
        return True
    return False


def reap_originals(database):
    """AI keeps its immutable source until done; LLM no longer needs media."""
    for row in database.recordings.find({"encode_status": "done", "original_file_path": {"$exists": True},
                                        "original_retired": {"$ne": True}, "keep_original": {"$ne": True}}).limit(50):
        if row.get("asr_enabled", True) and row.get("asr_state") not in {"done", "failed"} and row.get("status") not in {
            "asr_done", "summarizing", "summarized", "asr_failed", "summary_failed"
        }:
            continue
        original, current = row["original_file_path"], row.get("file_path")
        if not current or original == current:
            continue
        # Future re-transcription will use the published MP4. Existing readers
        # still hold their own lease on the old TS and prevent retirement.
        database.recordings.update_one({"_id": row["_id"], "file_path": current},
                                       {"$set": {"asr_source_path": current}})
        if retire_file(database, original):
            database.recordings.update_one({"_id": row["_id"], "file_path": current},
                                           {"$set": {"original_retired": True}})


def retire_files(database, paths, root=None):
    """Reserve every artifact before deleting any part of an archive."""
    root = Path(root or os.getenv("RECORDINGS_ROOT", "/data/recordings")).resolve()
    files = {Path(path).resolve() for path in paths if path}
    if any(not p.is_relative_to(root) or p == root for p in files):
        raise ValueError("archive artifact is outside recordings root")
    files = [p for p in files if p.is_file()]
    owner, acquired, now = uuid.uuid4().hex, [], datetime.now(UTC)
    for path in files:
        identity = key(path)
        try:
            database.media_readers.update_one({"_id": identity, "retired": {"$ne": True},
                "readers": {"$not": {"$elemMatch": {"until": {"$gt": now}}}}},
                {"$set": {"retired": True, "retire_owner": owner, "path": str(path)}}, upsert=True)
            acquired.append(identity)
        except DuplicateKeyError:
            saved = database.media_readers.find_one({"_id": identity}) or {}
            if saved.get("retired") is True:
                continue
            database.media_readers.update_many({"_id": {"$in": acquired}, "retire_owner": owner},
                {"$set": {"retired": False}, "$unset": {"retire_owner": ""}})
            return False
    for path in files:
        path.unlink(missing_ok=True)
    return True
