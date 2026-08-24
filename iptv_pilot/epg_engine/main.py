from __future__ import annotations

import gzip
import io
import logging
import os
import re
from datetime import UTC, datetime
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


def fetch_epg() -> tuple[str, bytes]:
    urls = [value.strip() for value in os.getenv("EPG_URLS", "").split(",") if value.strip()]
    timeout = float(os.getenv("EPG_REQUEST_TIMEOUT", "90"))
    if not urls:
        raise RuntimeError("EPG_URLS is empty")
    errors: list[str] = []
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url in urls:
            try:
                if url.startswith("file://") or url.startswith("/"):
                    path = Path(url[7:] if url.startswith("file://") else url)
                    return url, decode_payload(path.read_bytes(), url)
                response = client.get(url)
                response.raise_for_status()
                return url, decode_payload(response.content, url)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                logger.warning("EPG source failed url=%s error=%s", url, exc)
    raise RuntimeError("all EPG sources failed: " + "; ".join(errors))


def ingest_once(database) -> None:
    source, payload = fetch_epg()
    channel_ops: list[UpdateOne] = []
    programme_ops: list[UpdateOne] = []
    channels = programmes = 0
    for kind, document in iter_xmltv(payload):
        if kind == "channel":
            channel_ops.append(
                UpdateOne(
                    {"channel_id": document["channel_id"]},
                    {
                        "$set": document,
                        "$setOnInsert": {"created_at": datetime.now(UTC), "enabled": False},
                    },
                    upsert=True,
                )
            )
            channels += 1
        else:
            programme_ops.append(
                UpdateOne(
                    {"channel_id": document["channel_id"], "start_time": document["start_time"]},
                    {"$set": document},
                    upsert=True,
                )
            )
            programmes += 1
        if len(channel_ops) >= 500:
            database.channels.bulk_write(channel_ops, ordered=False)
            channel_ops.clear()
        if len(programme_ops) >= 1000:
            database.epg_programs.bulk_write(programme_ops, ordered=False)
            programme_ops.clear()
    if channel_ops:
        database.channels.bulk_write(channel_ops, ordered=False)
    if programme_ops:
        database.epg_programs.bulk_write(programme_ops, ordered=False)
    disabled = database.channels.update_many(
        {"epg_source": "xmltv", "m3u_url": {"$exists": False}},
        {"$set": {"enabled": False}},
    )
    logger.info(
        "EPG ingest complete source=%s channels=%s programmes=%s epg_only_disabled=%s",
        source,
        channels,
        programmes,
        disabled.modified_count,
    )


def main() -> None:
    configure_logging()
    database = connect_database()
    interval = max(60, int(os.getenv("EPG_REFRESH_MINUTES", "30")) * 60)
    run_forever(lambda: ingest_once(database), interval)


if __name__ == "__main__":
    main()
