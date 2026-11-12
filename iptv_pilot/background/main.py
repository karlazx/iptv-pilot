"""Independent periodic jobs sharing one interpreter and MongoDB client."""

import logging
import os
import signal
import threading
from pathlib import Path

from iptv_pilot import __version__
from iptv_pilot.common.db import configure_logging, connect_database
from iptv_pilot.common.runtime import is_draining

LOG = logging.getLogger(__name__)


def jobs(database):
    from iptv_pilot.epg_engine.main import ingest_once
    from iptv_pilot.llm.main import run_once as llm
    from iptv_pilot.push.main import run_once as push
    from iptv_pilot.scheduler.main import schedule_once
    from scripts.channels.refresh_stream_quality import run_once as quality
    from scripts.channels.seed_channels import parse_m3u, sync_channels

    last_mtime = None

    def sync():
        nonlocal last_mtime
        path = Path(os.getenv("M3U_PATH", "/data/m3u/result.m3u"))
        if not path.is_file():
            return
        mtime = path.stat().st_mtime_ns
        if mtime == last_mtime:
            return
        rows = {}
        for row in parse_m3u(path):
            rows.setdefault(row["channel_id"], []).append(row)
        if rows:
            sync_channels(database, rows)
        last_mtime = mtime

    def seconds(key, default, minimum=5):
        return max(minimum, float(os.getenv(key, default)))

    return {
        "scheduler": (lambda: schedule_once(database), seconds("SCHEDULER_INTERVAL_SECONDS", "60")),
        "epg-engine": (lambda: ingest_once(database), seconds("EPG_REFRESH_MINUTES", "30") * 60),
        "stream-quality": (lambda: quality(int(os.getenv("STREAM_QUALITY_SAMPLE_COUNT", "2")),
                            float(os.getenv("STREAM_QUALITY_TIMEOUT_SECONDS", "20")),
                            int(os.getenv("STREAM_QUALITY_WORKERS", "3")), database=database),
                           seconds("STREAM_QUALITY_INTERVAL_SECONDS", "3600", 900)),
        "llm": (lambda: llm(database), seconds("LLM_POLL_SECONDS", "10")),
        "push": (lambda: push(database), seconds("PUSH_POLL_SECONDS", "10")),
        "channel-sync": (sync, seconds("CHANNEL_SYNC_INTERVAL_SECONDS", "300", 30)),
    }


def periodic(database, name, job, interval, stop):
    from datetime import UTC, datetime

    while not stop.is_set():
        if is_draining(database, "background") or is_draining(database, name):
            stop.wait(1)
            continue
        database.runtime_jobs.update_one({"_id": name}, {"$set": {
            "active": True, "host_service": "background", "version": __version__,
            "started_at": datetime.now(UTC), "last_error": None}}, upsert=True)
        try:
            job()
            database.runtime_jobs.update_one({"_id": name}, {"$set": {
                "last_success_at": datetime.now(UTC)}})
        except Exception as error:
            database.runtime_jobs.update_one({"_id": name}, {"$set": {
                "last_error": type(error).__name__, "last_error_at": datetime.now(UTC)}})
            LOG.exception("background job failed name=%s", name)
        finally:
            database.runtime_jobs.update_one({"_id": name}, {"$set": {"active": False}})
        stop.wait(interval)


def main():
    configure_logging()
    if "split" in os.getenv("RUNTIME_PROFILES", "").split(","):
        raise RuntimeError("compact and split profiles must not run together")
    database = connect_database()
    available = jobs(database)
    requested = set(os.getenv("BACKGROUND_JOBS", ",".join(available)).split(","))
    if not requested <= available.keys():
        raise ValueError("unknown BACKGROUND_JOBS entry")
    database.runtime_jobs.update_many({"host_service": "background"}, {"$set": {"active": False}})
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [threading.Thread(target=periodic,
               args=(database, name, *available[name], stop), name=name) for name in sorted(requested)]
    for thread in threads:
        thread.start()
    while not stop.wait(1):
        if any(not thread.is_alive() for thread in threads):
            stop.set()
            raise RuntimeError("a background scheduler thread exited unexpectedly")
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
