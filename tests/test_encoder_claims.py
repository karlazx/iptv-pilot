"""Exercise encoder claiming against MongoDB's real null/date query semantics."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pymongo import MongoClient

from iptv_pilot.recorder.encoder import claim_job


@pytest.fixture
def database():
    uri = os.getenv("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("TEST_MONGODB_URI is required for MongoDB integration tests")
    client = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    client.admin.command("ping")
    database = client[f"iptv_pilot_test_encoder_{uuid.uuid4().hex}"]
    try:
        yield database
    finally:
        client.drop_database(database.name)
        client.close()


def recording(**overrides):
    return {
        "file_path": "/recordings/example.ts",
        "status": "summarized",
        "encode_status": "pending",
        "completed_at": datetime.now(UTC),
        **overrides,
    }


@pytest.mark.parametrize("schedule", ["missing", "null", "past", "future"])
def test_claim_obeys_retry_schedule(database, schedule):
    row = recording()
    if schedule != "missing":
        row["next_encode_attempt_at"] = {
            "null": None,
            "past": datetime.now(UTC) - timedelta(hours=1),
            "future": datetime.now(UTC) + timedelta(hours=1),
        }[schedule]
    identifier = database.recordings.insert_one(row).inserted_id

    claimed = claim_job(database)

    if schedule == "future":
        assert claimed is None
        assert database.recordings.find_one({"_id": identifier})["encode_status"] == "pending"
    else:
        assert claimed["_id"] == identifier
        assert claimed["encode_status"] == "encoding"
        assert claimed["encode_claim_id"]
        # An active claim cannot be taken by the next worker.
        assert claim_job(database) is None


def test_claim_prioritizes_recent_completed_recordings(database):
    older = recording(completed_at=datetime.now(UTC) - timedelta(days=1))
    newer = recording(next_encode_attempt_at=None)
    old_id, new_id = database.recordings.insert_many([older, newer]).inserted_ids

    assert claim_job(database)["_id"] == new_id
    assert claim_job(database)["_id"] == old_id
    assert claim_job(database) is None


def test_claim_can_encode_independently_of_transcription(database):
    rid = database.recordings.insert_one(
        recording(status="completed", asr_enabled=True, next_encode_attempt_at=None)
    ).inserted_id

    assert claim_job(database)["_id"] == rid
    assert database.recordings.find_one({"_id": rid})["status"] == "completed"


def test_claim_recovers_stale_encoder_with_no_retry_delay(database):
    identifier = database.recordings.insert_one(
        recording(
            encode_status="encoding",
            encode_claim_id="stale-claim",
            encode_heartbeat_at=datetime.now(UTC) - timedelta(days=1),
            next_encode_attempt_at=None,
        )
    ).inserted_id

    claimed = claim_job(database)

    assert claimed["_id"] == identifier
    assert claimed["encode_claim_id"] != "stale-claim"
