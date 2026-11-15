"""Run DOM/API acceptance on synthetic data in an isolated disposable database."""

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pymongo import MongoClient


def seed(database):
    now = datetime.now(UTC)
    database.channels.insert_many([{"channel_id": f"c{i:03}", "name": f"Channel {i}",
        "enabled": True, "m3u_url": "http://example.invalid/live"} for i in range(61)])
    ids = database.recordings.insert_many([{"channel_id": f"c{i%61:03}",
        "program_title": "[needle]" if i == 204 else f"Story {i}", "start_time": now-timedelta(seconds=i),
        "status": "recording" if i == 0 else "summarized"} for i in range(205)]).inserted_ids
    database.summaries.insert_many([{"recording_id": ids[i], "program_title": f"Summary {i}",
        "channel_id": "c000", "summary_text": "Synthetic test summary", "ready_for_push": True,
        "created_at": now-timedelta(seconds=i)} for i in range(130)])
    database.subscriptions.insert_one({"name": "Rule", "match_type": "keyword", "keyword": "News", "enabled": True})
    from iptv_pilot.sources.models import make_source

    database.sources.insert_many([make_source(channel_id=f"c{i:03}", url=f"http://example.invalid/{i}",
        label=f"Source {i}", origin="custom", locked=True) for i in range(61)])
    database.bookmarks.insert_many([{"user": "default", "item_type": "recording", "item_id": ids[i],
        "created_at": now-timedelta(seconds=i)} for i in range(61)])
    database.push_events.insert_many([{"provider": "email", "status": "delivered",
        "updated_at": now-timedelta(seconds=i)} for i in range(61)])


def main():
    uri = os.environ.get("TEST_MONGODB_URI")
    if not uri:
        raise SystemExit("TEST_MONGODB_URI required; never use production data")
    client = MongoClient(uri, tz_aware=True)
    database = client["iptv_ui_test_"+uuid.uuid4().hex]
    process = None
    with tempfile.TemporaryDirectory(prefix="iptv-ui-") as temporary:
        try:
            seed(database)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = {**os.environ, "MONGODB_URI": uri, "MONGODB_DATABASE": database.name, "WEB_API_TOKEN": "",
                   "RECORDINGS_ROOT": temporary, "MEDIA_CACHE_DIR": str(Path(temporary)/"cache"),
                   "UI_TEST_BASE": f"http://127.0.0.1:{port}"}
            with (Path(temporary)/"server.log").open("w") as log:
                process = subprocess.Popen([sys.executable, "-m", "uvicorn", "iptv_pilot.web.main:app",
                    "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=log, stderr=log)
                for _ in range(100):
                    try:
                        urllib.request.urlopen(env["UI_TEST_BASE"]+"/health", timeout=1).close()
                        break
                    except Exception:
                        if process.poll() is not None:
                            raise RuntimeError("isolated web process exited")
                        time.sleep(.1)
                else:
                    raise RuntimeError("isolated web readiness timed out")
                subprocess.run(["node", "tests/frontend/library.cjs"], env=env, check=True)
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            client.drop_database(database.name)
            client.close()


if __name__ == "__main__":
    main()
