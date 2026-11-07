from __future__ import annotations

import gzip
import io
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
from pymongo import UpdateOne

from iptv_pilot.common.db import configure_logging, connect_database, run_forever

logger = logging.getLogger(__name__)


def parse_xmltv_time(value: str) -> datetime:
    cleaned = value.strip()
    match = re.match(r"^(\d{14})(?:\s*([+-]\d{4}))?", cleaned)
    if not match:
        raise ValueError(f"invalid XMLTV datetime: {value!r}")
    compact, offset = match.groups()
    parsed = datetime.strptime(compact + (offset or "+0000"), "%Y%m%d%H%M%S%z")
    return parsed.astimezone(UTC)


def decode_payload(payload: bytes, url: str) -> bytes:
    if payload.startswith(b"\x1f\x8b") or url.endswith(".gz"):
        return gzip.decompress(payload)
    return payload


def iter_xmltv(payload: bytes):
    for _, element in ElementTree.iterparse(io.BytesIO(payload), events=("end",)):
        if element.tag == "channel":
            display_name = element.findtext("display-name") or element.attrib.get("id", "")
            yield "channel", {
                "channel_id": element.attrib["id"],
                "name": display_name.strip(),
                "epg_source": "xmltv",
                "updated_at": datetime.now(UTC),
            }
            element.clear()
        elif element.tag == "programme":
            try:
                document = {
                    "channel_id": element.attrib["channel"],
                    "start_time": parse_xmltv_time(element.attrib["start"]),
                    "end_time": parse_xmltv_time(element.attrib["stop"]),
                    "title": (element.findtext("title") or "未命名节目").strip(),
                    "description": (element.findtext("desc") or "").strip(),
                    "raw": {
                        "category": [node.text for node in element.findall("category") if node.text],
                        "source_attrs": dict(element.attrib),
                    },
                }
                yield "programme", document
            finally:
                element.clear()


def fetch_source(url: str, timeout: float) -> bytes:
    """Fetch one XMLTV source, or raise."""
    if url.startswith("file://") or url.startswith("/"):
        path = Path(url[7:] if url.startswith("file://") else url)
        return decode_payload(path.read_bytes(), url)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return decode_payload(response.content, url)


def fetch_epg_sources() -> list[tuple[str, bytes]]:
    """Fetch every configured source, skipping the ones that fail.

    Sources are merged later, so a broken or stale endpoint only costs its own
    channels instead of hiding every other source behind a first-match rule.
    """
    urls = [value.strip() for value in os.getenv("EPG_URLS", "").split(",") if value.strip()]
    timeout = float(os.getenv("EPG_REQUEST_TIMEOUT", "90"))
    if not urls:
        raise RuntimeError("EPG_URLS is empty")
    payloads: list[tuple[str, bytes]] = []
    errors: list[str] = []
    for url in urls:
        try:
            payloads.append((url, fetch_source(url, timeout)))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            logger.warning("EPG source failed url=%s error=%s", url, exc)
    if not payloads:
        raise RuntimeError("all EPG sources failed: " + "; ".join(errors))
    return payloads


def fetch_epg() -> tuple[str, bytes]:
    """Return the first usable source; retained for callers and tests."""
    source, payload = fetch_epg_sources()[0]
    return source, payload


def ingest_once(database) -> None:
    payloads = fetch_epg_sources()
    channel_ops: list[UpdateOne] = []
    programme_ops: list[UpdateOne] = []
    channels = programmes = 0
    # Earlier sources win on duplicate (channel, start) pairs, so list them in
    # priority order and let the first writer own each programme.
    seen_programmes: set[tuple[str, datetime]] = set()
    seen_channels: set[str] = set()
    for source, payload in payloads:
        source_channels = source_programmes = 0
        for kind, document in iter_xmltv(payload):
            if kind == "channel":
                key = document["channel_id"]
                if key in seen_channels:
                    continue
                seen_channels.add(key)
                channel_ops.append(
                    UpdateOne(
                        {"channel_id": key},
                        {
                            "$set": document,
                            "$setOnInsert": {"created_at": datetime.now(UTC), "enabled": False},
                        },
                        upsert=True,
                    )
                )
                channels += 1
                source_channels += 1
            else:
                key = (document["channel_id"], document["start_time"])
                if key in seen_programmes:
                    continue
                seen_programmes.add(key)
                programme_ops.append(
                    UpdateOne(
                        {"channel_id": document["channel_id"], "start_time": document["start_time"]},
                        {"$set": document},
                        upsert=True,
                    )
                )
                programmes += 1
                source_programmes += 1
            if len(channel_ops) >= 500:
                database.channels.bulk_write(channel_ops, ordered=False)
                channel_ops.clear()
            if len(programme_ops) >= 1000:
                database.epg_programs.bulk_write(programme_ops, ordered=False)
                programme_ops.clear()
        logger.info(
            "EPG source ingested source=%s channels=%s programmes=%s",
            source,
            source_channels,
            source_programmes,
        )
    if channel_ops:
        database.channels.bulk_write(channel_ops, ordered=False)
    if programme_ops:
        database.epg_programs.bulk_write(programme_ops, ordered=False)
    disabled = database.channels.update_many(
        {"epg_source": "xmltv", "m3u_url": {"$exists": False}},
        {"$set": {"enabled": False}},
    )
    coverage = upcoming_coverage(database)
    logger.info(
        "EPG ingest complete sources=%s channels=%s programmes=%s epg_only_disabled=%s "
        "upcoming_24h=%s",
        len(payloads),
        channels,
        programmes,
        disabled.modified_count,
        coverage,
    )
    if coverage == 0:
        logger.error(
            "EPG ingest produced no programmes in the next 24 hours; "
            "every configured source may be stale: %s",
            ", ".join(source for source, _ in payloads),
        )


def upcoming_coverage(database, hours: int = 24) -> int:
    now = datetime.now(UTC)
    return database.epg_programs.count_documents(
        {"start_time": {"$gte": now, "$lte": now + timedelta(hours=hours)}}
    )


def main() -> None:
    configure_logging()
    database = connect_database()
    interval = max(60, int(os.getenv("EPG_REFRESH_MINUTES", "30")) * 60)
    run_forever(lambda: ingest_once(database), interval)


if __name__ == "__main__":
    main()
