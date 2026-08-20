from __future__ import annotations

import os
import re
import secrets
import time as time_module
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import uvicorn
import httpx
from bson import ObjectId
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from pymongo import ReturnDocument

from services.common.db import configure_logging, connect_database
from services.common.live_paths import channel_cache_directory
from services.web.media import (
    MediaNotFoundError,
    MediaPathError,
    MediaPreparationError,
    download_media_type,
    prepare_browser_media,
    resolve_recording_file,
)
from services.web.live import (
    LiveProxyError,
    LiveTokenSigner,
    live_media_type,
    open_live_response,
    read_live_response,
    response_is_playlist,
    rewrite_hls_manifest,
)

database = None
live_http_client: httpx.AsyncClient | None = None
live_signer = LiveTokenSigner(lifetime_seconds=int(os.getenv("LIVE_TOKEN_TTL_SECONDS", "900")))
normalized_live_root = Path(os.getenv("LIVE_NORMALIZE_ROOT", "/data/media-cache/live-hls"))
normalized_stale_seconds = max(30, int(os.getenv("LIVE_NORMALIZE_STALE_SECONDS", "60")))
normalized_channels = {
    value.strip()
    for value in os.getenv("LIVE_NORMALIZE_CHANNELS", "CCTV-1").split(",")
    if value.strip()
}

# Live buffer profiles. ``buffer_duration`` maps to hls.js ``maxBufferLength``
# and ``live_sync`` to ``liveSyncDurationCount``. The balanced profile matches
# the historical hard-coded defaults (60s / 10 segments).
LIVE_BUFFER_PROFILES = {
    "low": {"label": "低延迟", "buffer_duration": 15, "live_sync": 4},
    "balanced": {"label": "平衡", "buffer_duration": 60, "live_sync": 10},
    "smooth": {"label": "流畅", "buffer_duration": 120, "live_sync": 15},
}
LIVE_BUFFER_DEFAULT = "balanced"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global database, live_http_client
    configure_logging()
    database = connect_database()
    live_http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(float(os.getenv("LIVE_PROXY_TIMEOUT_SECONDS", "20"))),
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20, keepalive_expiry=60),
    )
    try:
        yield
    finally:
        await live_http_client.aclose()
        live_http_client = None


app = FastAPI(title="IPTV 智能管家 API", version="0.3.0", lifespan=lifespan)
static_directory = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=static_directory), name="static")

_origins = [
    value.strip() for value in os.getenv("WEB_CORS_ORIGINS", "").split(",") if value.strip()
]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class TimeRule(BaseModel):
    weekday: int | None = Field(default=None, ge=0, le=6)
    weekdays: list[int] | None = None
    time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    tolerance_min: int = Field(default=0, ge=0, le=180)
    timezone: str = "Asia/Shanghai"

    @model_validator(mode="after")
    def validate_weekdays(self):
        if self.weekdays is not None and any(value < 0 or value > 6 for value in self.weekdays):
            raise ValueError("weekdays values must be 0..6")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError("invalid timezone") from exc
        return self


class SubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    match_type: Literal["keyword", "channel_time"]
    keyword: str | None = None
    channel_id: str | None = None
    time_rule: TimeRule | None = None
    asr_enabled: bool = True
    asr_model: Literal["tiny", "base", "small", "medium"] = "small"
    llm_enabled: bool = True
    push_enabled: bool = True
    auto_bookmark: bool = False
    post_roll_seconds: int = Field(default=180, ge=60, le=600)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_match(self):
        if self.match_type == "keyword" and not (self.keyword or "").strip():
            raise ValueError("keyword is required")
        if self.match_type == "channel_time" and (not self.channel_id or not self.time_rule):
            raise ValueError("channel_id and time_rule are required")
        return self


class SubscriptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    match_type: Literal["keyword", "channel_time"] | None = None
    keyword: str | None = None
    channel_id: str | None = None
    time_rule: TimeRule | None = None
    asr_enabled: bool | None = None
    asr_model: Literal["tiny", "base", "small", "medium"] | None = None
    llm_enabled: bool | None = None
    push_enabled: bool | None = None
    auto_bookmark: bool | None = None
    post_roll_seconds: int | None = Field(default=None, ge=60, le=600)
    enabled: bool | None = None


class ManualRecordingCreate(BaseModel):
    channel_id: str
    duration_sec: int = Field(default=30, ge=10, le=600)
    program_title: str = Field(default="Phase 3 端到端测试", min_length=1, max_length=100)
    asr_enabled: bool = True
    asr_model: Literal["tiny", "base", "small", "medium"] = "base"
    llm_enabled: bool = True
    push_enabled: bool = True


class BookmarkCreate(BaseModel):
    user: str = "default"
    item_type: Literal["recording", "summary"]
    item_id: str
    note: str = ""


class StreamSelection(BaseModel):
    index: int | None = None


class ChannelAdminUpdate(BaseModel):
    name_override: str | None = Field(default=None, max_length=100)
    category_override: str | None = Field(default=None, max_length=40)
    sort_weight: int | None = Field(default=None, ge=-100000, le=100000)
    disabled: bool | None = None

    @model_validator(mode="after")
    def at_least_one(self):
        fields = self.model_fields_set
        if not any(key in fields for key in ("name_override", "category_override", "sort_weight", "disabled")):
            raise ValueError("no channel admin fields provided")
        return self


class ChannelBatchUpdate(BaseModel):
    channel_ids: list[str] = Field(min_length=1, max_length=200)
    disabled: bool


class ChannelCreate(BaseModel):
    channel_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    m3u_url: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="地方与其他", max_length=40)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_url(self):
        parsed = urlparse(self.m3u_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("m3u_url must be an http(s) URL")
        return self


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    note: str = Field(default="", max_length=200)
    enabled: bool = True
    channel_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_url(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an http(s) URL")
        return self


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    note: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    channel_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_url(self):
        if self.url is not None:
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("url must be an http(s) URL")
        return self


class SourceTestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class BufferProfileUpdate(BaseModel):
    profile: Literal["low", "balanced", "smooth"]


def serialize(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize(item) for key, item in value.items()}
    return value


def object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid ObjectId") from exc


@app.middleware("http")
async def api_authentication(request: Request, call_next):
    token = os.getenv("WEB_API_TOKEN", "")
    if token and request.url.path.startswith("/api/"):
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse(status_code=401, content={"detail": "invalid API token"})
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return static_directory.joinpath("index.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict:
    database.command("ping")
    return {"status": "ok", "time": datetime.now(UTC).isoformat()}


_CHANNEL_CATEGORY_ORDER = ["央视频道", "卫视频道", "国际频道", "地方与其他"]


def _default_category(channel: dict) -> str:
    """Derive a channel's default category from its M3U group-title or name."""
    group = (channel.get("raw_m3u") or {}).get("group-title") or ""
    cleaned = re.sub(r"^[^\w一-鿿]+", "", group.strip())
    name = f"{channel.get('name', '')} {channel.get('channel_id', '')}".lower()
    if "央视" in cleaned or re.match(r"^(cctv|cgtn)", channel.get("channel_id", ""), re.I):
        return "央视频道"
    if "国际" in cleaned or re.search(r"nhk|bbc|cna|al jazeera|france 24|arirang|cgtn", name):
        return "国际频道"
    if "卫视" in cleaned or "卫视" in name:
        return "卫视频道"
    return "地方与其他"


def channel_display(channel: dict) -> dict:
    """Apply user overrides (name/category) and expose derived category for the frontend."""
    result = dict(channel)
    result["name"] = channel.get("name_override") or channel.get("name") or channel.get("channel_id")
    result["category"] = channel.get("category_override") or _default_category(channel)
    return result


@app.get("/api/channels")
def channels(category: str | None = None, include_disabled: bool = False) -> list[dict]:
    query: dict[str, Any] = {}
    if not include_disabled:
        query.update({"enabled": True, "disabled": {"$ne": True}, "m3u_url": {"$exists": True}})
    rows = [
        channel_display(row)
        for row in database.channels.find(
            query, {"raw_m3u": 0, "stream_candidates": 0, "stream_quality": 0}
        )
    ]
    if category:
        rows = [row for row in rows if row.get("category") == category]
    rows.sort(key=lambda row: (row.get("sort_weight") is None, -(row.get("sort_weight") or 0), row.get("name", "")))
    return serialize(rows)


def enabled_channel(channel_id: str) -> dict:
    channel = database.channels.find_one(
        {"channel_id": channel_id, "enabled": True, "disabled": {"$ne": True}, "m3u_url": {"$exists": True}}
    )
    if not channel:
        raise HTTPException(status_code=404, detail="channel is not available for live viewing")
    return channel


@app.get("/api/channels/admin")
def channels_admin() -> list[dict]:
    """Full channel list for the management page, including disabled and stream-less rows."""
    rows = [
        channel_display(row)
        for row in database.channels.find({}, {"stream_quality": 0}).sort("name", 1)
    ]
    for row in rows:
        row["stream_count"] = len(row.get("stream_candidates") or [])
        row["epg_matched"] = row.get("epg_source") == "xmltv"
        row.pop("stream_candidates", None)
        row.pop("raw_m3u", None)
    return serialize(rows)


@app.patch("/api/channels/{channel_id}")
def update_channel_admin(channel_id: str, payload: ChannelAdminUpdate) -> dict:
    current = database.channels.find_one({"channel_id": channel_id})
    if not current:
        raise HTTPException(status_code=404, detail="channel not found")
    update: dict[str, Any] = {}
    for key in ("name_override", "category_override", "sort_weight", "disabled"):
        if key in payload.model_fields_set:
            value = getattr(payload, key)
            if value is None:
                update["$unset"] = {**(update.get("$unset") or {}), key: ""}
            else:
                update.setdefault("$set", {})[key] = value
    update.setdefault("$set", {})["updated_at"] = datetime.now(UTC)
    database.channels.update_one({"channel_id": channel_id}, update)
    row = database.channels.find_one({"channel_id": channel_id})
    display = channel_display(row)
    display["stream_count"] = len(row.get("stream_candidates") or [])
    return serialize(display)


@app.post("/api/channels/batch")
def batch_channel_admin(payload: ChannelBatchUpdate) -> dict:
    result = database.channels.update_many(
        {"channel_id": {"$in": payload.channel_ids}},
        {"$set": {"disabled": payload.disabled, "updated_at": datetime.now(UTC)}},
    )
    return {"matched": result.matched_count, "modified": result.modified_count}


@app.post("/api/channels", status_code=201)
def create_channel(payload: ChannelCreate) -> dict:
    existing = database.channels.find_one({"channel_id": payload.channel_id})
    if existing:
        raise HTTPException(
            status_code=409,
            detail="channel_id already exists and would be overwritten by the next seed sync",
        )
    document = payload.model_dump()
    document["source"] = "manual"
    document["created_at"] = datetime.now(UTC)
    document["updated_at"] = document["created_at"]
    document["stream_candidates"] = [document["m3u_url"]]
    database.channels.insert_one(document)
    row = database.channels.find_one({"channel_id": payload.channel_id})
    display = channel_display(row)
    display["stream_count"] = 1
    return serialize(display)


@app.delete("/api/channels/{channel_id}", status_code=204, response_class=Response)
def delete_channel(channel_id: str) -> Response:
    current = database.channels.find_one({"channel_id": channel_id})
    if not current:
        raise HTTPException(status_code=404, detail="channel not found")
    if current.get("source") == "iptv-api":
        raise HTTPException(
            status_code=409,
            detail="seed 同步的内置频道不可删除，仅可禁用",
        )
    active_subscriptions = database.subscriptions.count_documents(
        {"channel_id": channel_id, "enabled": True}, limit=1
    )
    if active_subscriptions:
        raise HTTPException(
            status_code=409,
            detail="该频道存在未结束的订阅，无法删除；请先停用或删除相关订阅",
        )
    database.channels.delete_one({"channel_id": channel_id})
    return Response(status_code=204)


@app.get("/api/channels/{channel_id}/detail")
def channel_detail(channel_id: str) -> dict:
    """Channel detail drawer: stream list, recent recordings, EPG match status."""
    channel = database.channels.find_one(
        {"channel_id": channel_id},
        {"raw_m3u": 1, "stream_candidates": 1, "stream_quality": 1, "stream_manual": 1},
    )
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    recordings = list(
        database.recordings.find(
            {"channel_id": channel_id}, {"file_path": 1, "start_time": 1, "status": 1, "program_title": 1, "duration_sec": 1}
        ).sort("start_time", -1).limit(10)
    )
    epg_count = database.epg_programs.count_documents({"channel_id": channel_id})
    return serialize(
        {
            "channel": channel_display(channel),
            "streams": channel_stream_options(channel_id),
            "recent_recordings": recordings,
            "epg": {
                "matched": channel.get("epg_source") == "xmltv",
                "program_count": epg_count,
            },
        }
    )


def _merge_sources(channels: list[dict], custom_rows: list[dict]) -> list[dict]:
    """Merge seed-derived streams (m3u_url + candidates) with user custom_sources."""
    sources: list[dict] = []
    channel_name_by_id = {row["channel_id"]: (row.get("name") or row["channel_id"]) for row in channels}
    for channel in channels:
        current_url = channel.get("m3u_url")
        candidates = list(dict.fromkeys(channel.get("stream_candidates") or []))
        if current_url and current_url not in candidates:
            candidates.insert(0, current_url)
        for url in candidates:
            sources.append(
                {
                    "channel_id": channel["channel_id"],
                    "name": channel_name_by_id.get(channel["channel_id"], channel["channel_id"]),
                    "channel_name": channel_name_by_id.get(channel["channel_id"], channel["channel_id"]),
                    "url": url,
                    "source_type": "seed",
                    "note": "",
                    "enabled": True,
                    "current": url == current_url,
                }
            )
    for row in custom_rows:
        channel_id = row.get("channel_id")
        sources.append(
            {
                "_id": row["_id"],
                "name": row.get("name") or urlparse(row["url"]).netloc,
                "channel_id": channel_id,
                "channel_name": channel_name_by_id.get(channel_id) if channel_id else None,
                "url": row["url"],
                "source_type": "custom",
                "note": row.get("note", ""),
                "enabled": row.get("enabled", True),
                "current": False,
            }
        )
    return sources


@app.get("/api/sources")
def sources() -> dict:
    """Merged stream list: seed candidates + user custom_sources."""
    channels = list(
        database.channels.find(
            {}, {"channel_id": 1, "name": 1, "name_override": 1, "m3u_url": 1, "stream_candidates": 1}
        ).sort("channel_id", 1)
    )
    for row in channels:
        row["name"] = row.get("name_override") or row.get("name") or row["channel_id"]
    custom_rows = list(database.custom_sources.find().sort("created_at", -1))
    rows = _merge_sources(channels, custom_rows)
    latest_sync = database.channels.find_one(sort=[("updated_at", -1)])
    return serialize(
        {
            "m3u_summary": {
                "source": "iptv-api result.m3u",
                "last_sync_at": (latest_sync or {}).get("updated_at"),
                "seed_stream_count": sum(1 for row in rows if row["source_type"] == "seed"),
            },
            "sources": rows,
        }
    )


@app.post("/api/sources", status_code=201)
def create_source(payload: SourceCreate) -> dict:
    document = payload.model_dump()
    document["created_at"] = datetime.now(UTC)
    document["updated_at"] = document["created_at"]
    result = database.custom_sources.insert_one(document)
    return serialize(database.custom_sources.find_one({"_id": result.inserted_id}))


@app.patch("/api/sources/{source_id}")
def update_source(source_id: str, payload: SourceUpdate) -> dict:
    update = payload.model_dump(exclude_unset=True)
    if not update:
        raise HTTPException(status_code=422, detail="no source fields provided")
    unset: dict[str, str] = {}
    if "channel_id" in update and update["channel_id"] is None:
        unset["channel_id"] = ""
        update.pop("channel_id")
    update["updated_at"] = datetime.now(UTC)
    operation: dict[str, Any] = {"$set": update}
    if unset:
        operation["$unset"] = unset
    result = database.custom_sources.find_one_and_update(
        {"_id": object_id(source_id)}, operation, return_document=ReturnDocument.AFTER
    )
    if not result:
        raise HTTPException(status_code=404, detail="source not found")
    return serialize(result)


@app.delete("/api/sources/{source_id}", status_code=204, response_class=Response)
def delete_source(source_id: str) -> Response:
    result = database.custom_sources.delete_one({"_id": object_id(source_id)})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="source not found")
    return Response(status_code=204)


async def _probe_source(url: str) -> dict:
    """Probe an HLS source: fetch playlist + first media segment, return status/size/elapsed."""
    if live_http_client is None:
        raise HTTPException(status_code=503, detail="live proxy is not ready")
    started = time_module.perf_counter()
    try:
        upstream = await open_live_response(live_http_client, url)
        status = upstream.status_code
        if response_is_playlist(upstream):
            content = await read_live_response(upstream)
            text = content.decode("utf-8", errors="replace")
            segment_url = None
            for raw in text.splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    segment_url = urljoin(str(upstream.url), line)
                    break
            first_segment: dict[str, Any] | None = None
            if segment_url:
                segment_started = time_module.perf_counter()
                try:
                    seg = await open_live_response(live_http_client, segment_url)
                    seg_bytes = len(await read_live_response(seg))
                    first_segment = {
                        "status": seg.status_code,
                        "size_bytes": seg_bytes,
                        "elapsed_ms": int((time_module.perf_counter() - segment_started) * 1000),
                    }
                except LiveProxyError as exc:
                    first_segment = {"error": str(exc)}
            return {
                "ok": status == 200,
                "status": status,
                "content_type": upstream.headers.get("content-type", ""),
                "elapsed_ms": int((time_module.perf_counter() - started) * 1000),
                "first_segment": first_segment,
            }
        return {
            "ok": status == 200,
            "status": status,
            "content_type": upstream.headers.get("content-type", ""),
            "elapsed_ms": int((time_module.perf_counter() - started) * 1000),
            "first_segment": None,
        }
    except LiveProxyError as exc:
        return {
            "ok": False,
            "status": getattr(exc, "status_code", None),
            "error": str(exc),
            "elapsed_ms": int((time_module.perf_counter() - started) * 1000),
        }


@app.post("/api/sources/test")
async def test_source(payload: SourceTestRequest) -> dict:
    return await _probe_source(payload.url)


def channel_stream_options(channel_id: str) -> dict:
    channel = database.channels.find_one(
        {"channel_id": channel_id},
        {"m3u_url": 1, "stream_candidates": 1, "stream_quality": 1, "stream_manual": 1},
    )
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    candidates = list(dict.fromkeys(channel.get("stream_candidates") or []))
    current_url = channel.get("m3u_url")
    if current_url and current_url not in candidates:
        candidates.insert(0, current_url)
    current_index = next(
        (index for index, url in enumerate(candidates) if url == current_url), None
    )
    quality_results = (channel.get("stream_quality") or {}).get("results") or []
    quality_by_host = {
        row.get("host"): row for row in quality_results if isinstance(row, dict)
    }
    manual = channel.get("stream_manual")

    def candidate_summary(index: int, url: str) -> dict:
        return {
            "index": index,
            "host": urlparse(url).netloc,
            "current": index == current_index,
            "quality": quality_by_host.get(urlparse(url).netloc),
        }

    return {
        "channel_id": channel_id,
        "current_index": current_index,
        "manual": serialize(manual) if manual else None,
        "candidates": [
            candidate_summary(index, url) for index, url in enumerate(candidates)
        ],
    }


@app.get("/api/channels/{channel_id}/streams")
def channel_streams(channel_id: str) -> dict:
    return channel_stream_options(channel_id)


@app.post("/api/channels/{channel_id}/stream")
def select_channel_stream(channel_id: str, payload: StreamSelection) -> dict:
    channel = database.channels.find_one(
        {"channel_id": channel_id},
        {"m3u_url": 1, "stream_candidates": 1},
    )
    if not channel:
        raise HTTPException(status_code=404, detail="channel not found")
    candidates = list(dict.fromkeys(channel.get("stream_candidates") or []))
    current_url = channel.get("m3u_url")
    if current_url and current_url not in candidates:
        candidates.insert(0, current_url)
    if payload.index is None:
        update: dict[str, Any] = {"$unset": {"stream_manual": ""}}
    else:
        if payload.index < 0 or payload.index >= len(candidates):
            raise HTTPException(status_code=400, detail="stream index out of range")
        update = {
            "$set": {
                "m3u_url": candidates[payload.index],
                "stream_manual": {"index": payload.index, "at": datetime.now(UTC)},
            }
        }
    database.channels.update_one({"channel_id": channel_id}, update)
    return channel_stream_options(channel_id)


async def live_response(url: str) -> Response:
    if live_http_client is None:
        raise HTTPException(status_code=503, detail="live proxy is not ready")
    try:
        upstream = await open_live_response(live_http_client, url)
    except LiveProxyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    media_type = live_media_type(upstream)
    if response_is_playlist(upstream):
        try:
            content = await read_live_response(upstream)
        except LiveProxyError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not content.lstrip().startswith(b"#EXTM3U"):
            raise HTTPException(status_code=502, detail="live stream upstream returned an invalid HLS playlist")
        rewritten = rewrite_hls_manifest(
            content.decode("utf-8", errors="replace"), str(upstream.url), live_signer
        ).encode()
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def stream_upstream():
        try:
            async for chunk in upstream.aiter_raw(256 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    headers = {"Cache-Control": "private, max-age=30", "X-Content-Type-Options": "nosniff"}
    if upstream.headers.get("content-length"):
        headers["Content-Length"] = upstream.headers["content-length"]
    return StreamingResponse(stream_upstream(), media_type=media_type, headers=headers)


def normalized_live_manifest(channel_id: str) -> Response | None:
    if channel_id not in normalized_channels:
        return None
    directory = channel_cache_directory(normalized_live_root, channel_id)
    manifest = directory / "index.m3u8"
    if (
        not manifest.is_file()
        or datetime.now().timestamp() - manifest.stat().st_mtime > normalized_stale_seconds
    ):
        return None
    lines = []
    for raw_line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            filename = Path(line).name
            line = f"/api/channels/{channel_id}/normalized/{filename}"
        lines.append(line)
    return Response(
        content=("\n".join(lines) + "\n").encode(),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store", "X-Live-Mode": "normalized"},
    )


@app.get("/api/channels/{channel_id}/live")
async def watch_live_channel(channel_id: str) -> Response:
    channel = enabled_channel(channel_id)
    normalized = normalized_live_manifest(channel_id)
    if normalized is not None:
        return normalized
    return await live_response(channel["m3u_url"])


@app.get("/api/channels/{channel_id}/normalized/{filename}")
def normalized_live_segment(channel_id: str, filename: str) -> Response:
    if channel_id not in normalized_channels or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="normalized live segment not found")
    if not filename.endswith(".ts"):
        raise HTTPException(status_code=404, detail="normalized live segment not found")
    path = channel_cache_directory(normalized_live_root, channel_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="normalized live segment not found")
    return FileResponse(
        path,
        media_type="video/mp2t",
        headers={"Cache-Control": "private, max-age=20", "X-Live-Mode": "normalized"},
    )


@app.get("/api/live/proxy/{token}")
async def proxy_live_asset(token: str) -> Response:
    try:
        url = live_signer.verify(token)
    except LiveProxyError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return await live_response(url)


def local_day_window(target_date: date) -> tuple[datetime, datetime]:
    local_tz = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
    local_start = datetime.combine(target_date, time.min, tzinfo=local_tz)
    return local_start.astimezone(UTC), (local_start + timedelta(days=1)).astimezone(UTC)


@app.get("/api/epg")
def epg(channel: str, target_date: date = Query(alias="date")) -> list[dict]:
    start, end = local_day_window(target_date)
    query = {"channel_id": channel, "start_time": {"$gte": start, "$lt": end}}
    return serialize(list(database.epg_programs.find(query).sort("start_time", 1)))


@app.get("/api/epg/search")
def epg_search(
    q: str = Query(min_length=1),
    channel: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    # MongoDB text indexes do not segment Chinese programme names into useful
    # substrings. At the household EPG scale a safe escaped regex provides the
    # expected "新闻" -> "新闻联播" behaviour for both Chinese and Latin text.
    pattern = {"$regex": re.escape(q.strip()), "$options": "i"}
    query: dict[str, Any] = {"$or": [{"title": pattern}, {"description": pattern}]}
    if channel:
        query["channel_id"] = channel
    rows = (
        database.epg_programs.find(query)
        .sort("start_time", 1)
        .limit(limit)
    )
    return serialize(list(rows))


@app.get("/api/summaries/latest")
def latest_summaries(limit: int = Query(10, ge=1, le=100)) -> list[dict]:
    return serialize(list(database.summaries.find({}, {"embedding": 0}).sort("created_at", -1).limit(limit)))


@app.get("/api/summaries")
def summaries(target_date: date = Query(alias="date")) -> list[dict]:
    start, end = local_day_window(target_date)
    return serialize(
        list(database.summaries.find({"created_at": {"$gte": start, "$lt": end}}, {"embedding": 0}))
    )


@app.get("/api/summaries/{summary_id}")
def summary_detail(summary_id: str) -> dict:
    summary = database.summaries.find_one({"_id": object_id(summary_id)}, {"embedding": 0})
    if not summary:
        raise HTTPException(status_code=404, detail="summary not found")
    transcript = database.transcripts.find_one({"_id": summary.get("transcript_id")}, {"embedding": 0})
    return serialize({**summary, "transcript": transcript})


@app.get("/api/search")
def search(q: str = Query(min_length=1), vector: bool = False) -> dict:
    summary_rows = list(database.summaries.find({"$text": {"$search": q}}, {"embedding": 0}).limit(30))
    transcript_rows = list(
        database.transcripts.find(
            {"full_text": {"$regex": re.escape(q), "$options": "i"}},
            {"embedding": 0, "segments": 0},
        ).limit(30)
    )
    vector_rows: list[dict] = []
    vector_note = None
    if vector:
        try:
            from services.common.embedding import embed_text

            query_vector = embed_text(q)
            if query_vector is None:
                raise RuntimeError("EMBEDDING_BACKEND is disabled")
            vector_rows = list(
                database.transcripts.aggregate(
                    [
                        {
                            "$vectorSearch": {
                                "index": "transcript_vector",
                                "path": "embedding",
                                "queryVector": query_vector,
                                "numCandidates": 100,
                                "limit": 20,
                            }
                        },
                        {"$set": {"score": {"$meta": "vectorSearchScore"}}},
                        {"$unset": ["embedding", "segments"]},
                    ]
                )
            )
        except Exception as exc:
            vector_note = str(exc)
    return serialize(
        {
            "summaries": summary_rows,
            "transcripts": transcript_rows,
            "vector_transcripts": vector_rows,
            "vector_requested": vector,
            "vector_note": vector_note,
        }
    )


def ensure_enabled_channel(channel_id: str) -> None:
    if not database.channels.find_one(
        {"channel_id": channel_id, "enabled": True, "disabled": {"$ne": True}, "m3u_url": {"$exists": True}}
    ):
        raise HTTPException(status_code=422, detail="channel_id is not an enabled stream")


@app.post("/api/subscriptions", status_code=201)
def create_subscription(payload: SubscriptionCreate) -> dict:
    document = payload.model_dump()
    if document.get("channel_id"):
        ensure_enabled_channel(document["channel_id"])
    document["created_at"] = datetime.now(UTC)
    result = database.subscriptions.insert_one(document)
    return serialize(database.subscriptions.find_one({"_id": result.inserted_id}))


@app.get("/api/subscriptions")
def subscriptions() -> list[dict]:
    return serialize(list(database.subscriptions.find().sort("created_at", -1)))


@app.patch("/api/subscriptions/{subscription_id}")
def update_subscription(subscription_id: str, payload: SubscriptionUpdate) -> dict:
    current = database.subscriptions.find_one({"_id": object_id(subscription_id)})
    if not current:
        raise HTTPException(status_code=404, detail="subscription not found")
    update = payload.model_dump(exclude_unset=True)
    if not update:
        raise HTTPException(status_code=422, detail="no supported fields")
    merged = {key: current.get(key) for key in SubscriptionCreate.model_fields}
    merged.update(update)
    validated = SubscriptionCreate.model_validate(merged).model_dump()
    update = {key: validated[key] for key in update}
    if update.get("channel_id"):
        ensure_enabled_channel(update["channel_id"])
    update["updated_at"] = datetime.now(UTC)
    result = database.subscriptions.find_one_and_update(
        {"_id": current["_id"]}, {"$set": update}, return_document=ReturnDocument.AFTER
    )
    return serialize(result)


@app.delete("/api/subscriptions/{subscription_id}", status_code=204, response_class=Response)
def delete_subscription(subscription_id: str) -> Response:
    result = database.subscriptions.update_one(
        {"_id": object_id(subscription_id)},
        {"$set": {"enabled": False, "updated_at": datetime.now(UTC)}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="subscription not found")
    return Response(status_code=204)


@app.get("/api/recordings")
def recordings(status: str | None = None, limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    query = {"status": status} if status else {}
    return serialize(list(database.recordings.find(query).sort("start_time", -1).limit(limit)))


@app.post("/api/recordings/manual", status_code=201)
def create_manual_recording(payload: ManualRecordingCreate) -> dict:
    ensure_enabled_channel(payload.channel_id)
    now = datetime.now(UTC)
    document = {
        "subscription_id": None,
        "channel_id": payload.channel_id,
        "program_title": payload.program_title,
        "start_time": now,
        "end_time": now + timedelta(seconds=payload.duration_sec),
        "scheduled_start": now,
        "scheduled_end": now + timedelta(seconds=payload.duration_sec),
        "status": "queued",
        "retry_count": 0,
        "asr_enabled": payload.asr_enabled,
        "asr_model": payload.asr_model,
        "llm_enabled": payload.llm_enabled,
        "push_enabled": payload.push_enabled,
        "auto_bookmark": False,
        "manual": True,
        "created_at": now,
    }
    result = database.recordings.insert_one(document)
    return serialize(database.recordings.find_one({"_id": result.inserted_id}))


@app.get("/api/recordings/{recording_id}")
def recording_detail(recording_id: str) -> dict:
    recording = database.recordings.find_one({"_id": object_id(recording_id)})
    if not recording:
        raise HTTPException(status_code=404, detail="recording not found")
    transcript = database.transcripts.find_one(
        {"recording_id": recording["_id"]}, {"embedding": 0, "segments": 0}
    )
    summary = database.summaries.find_one(
        {"recording_id": recording["_id"]}, {"embedding": 0}
    )
    events = []
    if summary:
        events = list(database.push_events.find({"summary_id": summary["_id"]}))
    return serialize(
        {
            "recording": recording,
            "transcript": transcript,
            "summary": summary,
            "push_events": events,
        }
    )


def media_recording(recording_id: str) -> dict:
    recording = database.recordings.find_one({"_id": object_id(recording_id)})
    if not recording:
        raise HTTPException(status_code=404, detail="recording not found")
    if recording.get("status") in {"queued", "recording"}:
        raise HTTPException(status_code=409, detail="recording is not complete")
    return recording


@app.get("/api/recordings/{recording_id}/play", response_class=FileResponse)
def play_recording(recording_id: str) -> FileResponse:
    recording = media_recording(recording_id)
    try:
        asset = prepare_browser_media(
            recording_id,
            recording,
            Path(os.getenv("RECORDINGS_ROOT", "/data/recordings")),
            Path(os.getenv("MEDIA_CACHE_DIR", "/data/media-cache")),
            maximum_cache_bytes=int(os.getenv("MEDIA_CACHE_MAX_BYTES", str(20 * 1024**3))),
            timeout_seconds=int(os.getenv("MEDIA_REMUX_TIMEOUT_SECONDS", "300")),
        )
    except MediaNotFoundError as exc:
        raise HTTPException(status_code=404, detail="recording media is unavailable") from exc
    except MediaPathError as exc:
        raise HTTPException(status_code=404, detail="recording media is unavailable") from exc
    except MediaPreparationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(
        asset.path,
        media_type=asset.media_type,
        filename=f"{recording_id}.mp4",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/recordings/{recording_id}/download", response_class=FileResponse)
def download_recording(recording_id: str) -> FileResponse:
    recording = media_recording(recording_id)
    try:
        source = resolve_recording_file(
            recording, Path(os.getenv("RECORDINGS_ROOT", "/data/recordings"))
        )
    except (MediaNotFoundError, MediaPathError) as exc:
        raise HTTPException(status_code=404, detail="recording media is unavailable") from exc
    suffix = source.suffix.lower() or ".bin"
    return FileResponse(
        source,
        media_type=download_media_type(source),
        filename=f"{recording_id}{suffix}",
        content_disposition_type="attachment",
    )


@app.get("/api/push/events")
def push_events(limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    return serialize(list(database.push_events.find().sort("updated_at", -1).limit(limit)))


def _live_buffer_profile() -> dict:
    row = database.settings.find_one({"key": "live_buffer_profile"})
    profile = (row or {}).get("value", LIVE_BUFFER_DEFAULT)
    if profile not in LIVE_BUFFER_PROFILES:
        profile = LIVE_BUFFER_DEFAULT
    return {
        "profile": profile,
        "profiles": {
            key: {"key": key, **value}
            for key, value in LIVE_BUFFER_PROFILES.items()
        },
        "default": LIVE_BUFFER_DEFAULT,
    }


@app.get("/api/settings/buffer")
def get_buffer_profile() -> dict:
    return serialize(_live_buffer_profile())


@app.patch("/api/settings/buffer")
def set_buffer_profile(payload: BufferProfileUpdate) -> dict:
    database.settings.update_one(
        {"key": "live_buffer_profile"},
        {"$set": {"value": payload.profile, "updated_at": datetime.now(UTC)}},
        upsert=True,
    )
    return serialize(_live_buffer_profile())


@app.get("/api/system/status")
def system_status() -> dict:
    return {
        "channels": database.channels.count_documents({"enabled": True, "disabled": {"$ne": True}, "m3u_url": {"$exists": True}}),
        "epg_programs": database.epg_programs.count_documents({}),
        "subscriptions": database.subscriptions.count_documents({"enabled": True}),
        "recordings_by_status": {
            row["_id"]: row["count"]
            for row in database.recordings.aggregate(
                [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
            )
        },
        "latest_summary_at": serialize(
            (database.summaries.find_one(sort=[("created_at", -1)]) or {}).get("created_at")
        ),
        "normalizer_pool": _normalizer_pool(),
    }


def _normalizer_pool() -> dict:
    """Describe the live-normalizer pool: manually pinned channels and their health."""
    rows = []
    for channel_id in sorted(normalized_channels):
        channel = database.channels.find_one(
            {"channel_id": channel_id},
            {"name": 1, "name_override": 1, "normalizer_quality": 1},
        )
        quality = (channel or {}).get("normalizer_quality") or {}
        rows.append(
            {
                "channel_id": channel_id,
                "name": (channel or {}).get("name_override")
                or (channel or {}).get("name")
                or channel_id,
                "manual": True,
                "healthy": quality.get("healthy"),
                "measured_at": quality.get("measured_at"),
                "segment_timing": quality.get("segment_timing"),
            }
        )
    return {"channels": rows, "max_concurrency": int(os.getenv("LIVE_NORMALIZE_MAX_CHANNELS", "2"))}


@app.post("/api/bookmarks", status_code=201)
def create_bookmark(payload: BookmarkCreate) -> dict:
    document = payload.model_dump()
    document["item_id"] = object_id(document["item_id"])
    document["created_at"] = datetime.now(UTC)
    result = database.bookmarks.update_one(
        {"user": document["user"], "item_type": document["item_type"], "item_id": document["item_id"]},
        {"$set": document},
        upsert=True,
    )
    bookmark = database.bookmarks.find_one(
        {"_id": result.upserted_id}
        if result.upserted_id
        else {"user": document["user"], "item_type": document["item_type"], "item_id": document["item_id"]}
    )
    return serialize(bookmark)


@app.get("/api/bookmarks")
def bookmarks(user: str = "default") -> list[dict]:
    return serialize(list(database.bookmarks.find({"user": user}).sort("created_at", -1)))


@app.delete("/api/bookmarks/{bookmark_id}", status_code=204, response_class=Response)
def delete_bookmark(bookmark_id: str) -> Response:
    result = database.bookmarks.delete_one({"_id": object_id(bookmark_id)})
    if not result.deleted_count:
        raise HTTPException(status_code=404, detail="bookmark not found")
    return Response(status_code=204)


def main() -> None:
    uvicorn.run(
        "services.web.main:app",
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
