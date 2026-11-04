"""Reusable M3U parsing and channel-name normalization for source import."""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')
QUALITY_SUFFIX = re.compile(r"(超高清|高清|标清|HD|SD)$", re.IGNORECASE)


def normalize_channel_name(value: str) -> str:
    """Fold cosmetic differences so M3U entries can be matched to channels."""
    cleaned = re.sub(r"[\s\-_·]+", "", (value or "").upper().strip())
    # Channel names are commonly prefixed with a channel number.
    cleaned = re.sub(r"^\d+", "", cleaned)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = QUALITY_SUFFIX.sub("", cleaned)
    return cleaned


@dataclass
class M3UEntry:
    name: str
    url: str
    channel_id: str
    attributes: dict = field(default_factory=dict)
    group: str = ""

    @property
    def normalized_name(self) -> str:
        return normalize_channel_name(self.name)

    @property
    def normalized_channel_id(self) -> str:
        return normalize_channel_name(self.channel_id)


def parse_m3u_text(text: str) -> list[M3UEntry]:
    entries: list[M3UEntry] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attributes = dict(ATTRIBUTE.findall(line))
            name = line.rsplit(",", 1)[-1].strip()
            if "更新时间" in attributes.get("group-title", ""):
                current = None
                continue
            current = {
                "name": name,
                "channel_id": attributes.get("tvg-id") or attributes.get("tvg-name") or name,
                "attributes": attributes,
                "group": attributes.get("group-title", ""),
            }
        elif current and not line.startswith("#"):
            if _is_http_url(line):
                entries.append(
                    M3UEntry(
                        name=current["name"],
                        url=line,
                        channel_id=current["channel_id"],
                        attributes=current["attributes"],
                        group=current["group"],
                    )
                )
            current = None
    return entries


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def read_source(source: str, timeout: float = 30.0) -> str:
    """Read an M3U from a local path or an HTTP(S) URL, transparently ungzipping."""
    if source.startswith(("http://", "https://")):
        request = Request(source, headers={"User-Agent": "IPTVPilot/1.0"})
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    else:
        path = Path(source[7:] if source.startswith("file://") else source)
        payload = path.read_bytes()
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)
    return payload.decode("utf-8-sig", errors="replace")


@dataclass
class MatchResult:
    entry: M3UEntry
    channel_id: str | None
    matched_by: str | None


def match_entries(
    entries: list[M3UEntry],
    channels: list[dict],
    *,
    by: tuple[str, ...] = ("tvg_id", "name", "normalized_name"),
) -> list[MatchResult]:
    """Match M3U entries to channels, never guessing across ambiguous names."""
    exact_id: dict[str, str] = {}
    exact_name: dict[str, str] = {}
    normalized: dict[str, str] = {}
    ambiguous: set[str] = set()

    for channel in channels:
        channel_id = channel.get("channel_id")
        if not channel_id:
            continue
        labels = {
            channel_id,
            channel.get("name") or "",
            channel.get("name_override") or "",
        }
        exact_id[channel_id] = channel_id
        for label in labels:
            if label:
                exact_name.setdefault(label, channel_id)
        for label in labels:
            key = normalize_channel_name(label)
            if not key:
                continue
            if key in normalized and normalized[key] != channel_id:
                ambiguous.add(key)
            else:
                normalized[key] = channel_id

    results: list[MatchResult] = []
    for entry in entries:
        channel_id = None
        matched_by = None
        if "tvg_id" in by and entry.channel_id in exact_id:
            channel_id, matched_by = exact_id[entry.channel_id], "tvg_id"
        if channel_id is None and "name" in by and entry.name in exact_name:
            channel_id, matched_by = exact_name[entry.name], "name"
        if channel_id is None and "normalized_name" in by:
            key = entry.normalized_name
            if key and key not in ambiguous:
                target = normalized.get(key)
                if target is None and entry.normalized_channel_id != key:
                    target = normalized.get(entry.normalized_channel_id)
                if target:
                    channel_id, matched_by = target, "normalized_name"
        results.append(MatchResult(entry=entry, channel_id=channel_id, matched_by=matched_by))
    return results
