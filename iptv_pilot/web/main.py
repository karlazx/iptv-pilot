from __future__ import annotations

import logging
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

import httpx
import uvicorn
from bson import ObjectId
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from pymongo import ReturnDocument

from iptv_pilot import __version__
from iptv_pilot.common.db import configure_logging, connect_database
from iptv_pilot.common.live_paths import channel_cache_directory
from iptv_pilot.common.media_refs import MediaBusy, MediaReader
from iptv_pilot.push import settings as push_settings
from iptv_pilot.push.providers import EmailProvider
from iptv_pilot.recorder import encoding as recording_encoding
from iptv_pilot.sources import resolver, store
from iptv_pilot.sources.importing import apply_import, preview_import
from iptv_pilot.sources.models import source_public, url_key
from iptv_pilot.web.live import (
    LiveProxyError,
    LiveTokenSigner,
    live_media_type,
    open_live_response,
    read_live_response,
    response_is_playlist,
    rewrite_hls_manifest,
)
from iptv_pilot.web.media import (
    MediaNotFoundError,
    MediaPathError,
    MediaPreparationError,
    download_media_type,
    prepare_browser_media,
    resolve_recording_file,
)

database = None
live_http_client: httpx.AsyncClient | None = None
live_signer = LiveTokenSigner(lifetime_seconds=int(os.getenv("LIVE_TOKEN_TTL_SECONDS", "900")))
normalized_live_root = Path(os.getenv("LIVE_NORMALIZE_ROOT", "/data/media-cache/live-hls"))
normalized_stale_seconds = max(30, int(os.getenv("LIVE_NORMALIZE_STALE_SECONDS", "60")))
normalized_channels = {
    value.strip()
    for value in os.getenv("LIVE_NORMALIZE_CHANNELS", "").split(",")
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
    from iptv_pilot.llm.traceable import setup as setup_analysis

    setup_analysis(database)
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


app = FastAPI(title="IPTVPilot API", version=__version__, lifespan=lifespan)
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
            raise ValueError("星期值必须在 0 到 6 之间")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError("时区无效") from exc
        return self


class SubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    match_type: Literal["keyword", "channel_time"]
    keyword: str | None = None
    channel_id: str | None = None
    time_rule: TimeRule | None = None
    asr_enabled: bool = True
    live_asr_enabled: bool = True
    asr_model: Literal["tiny", "base", "small", "medium"] = "small"
    llm_enabled: bool = True
    push_enabled: bool = True
    auto_bookmark: bool = False
    post_roll_seconds: int = Field(default=180, ge=60, le=600)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_match(self):
        if self.match_type == "keyword" and not (self.keyword or "").strip():
            raise ValueError("关键词不能为空")
        if self.match_type == "channel_time" and (not self.channel_id or not self.time_rule):
            raise ValueError("频道和时间规则不能为空")
        return self


class SubscriptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    match_type: Literal["keyword", "channel_time"] | None = None
    keyword: str | None = None
    channel_id: str | None = None
    time_rule: TimeRule | None = None
    asr_enabled: bool | None = None
    live_asr_enabled: bool | None = None
    asr_model: Literal["tiny", "base", "small", "medium"] | None = None
    llm_enabled: bool | None = None
    push_enabled: bool | None = None
    auto_bookmark: bool | None = None
    post_roll_seconds: int | None = Field(default=None, ge=60, le=600)
    enabled: bool | None = None


class ManualRecordingCreate(BaseModel):
    channel_id: str
    duration_sec: int = Field(default=30, ge=10, le=600)
    program_title: str = Field(default="手工录制", min_length=1, max_length=100)
    asr_enabled: bool = True
    live_asr_enabled: bool = True
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
            raise ValueError("没有提供可更新的频道字段")
        return self


class ChannelBatchUpdate(BaseModel):
    channel_ids: list[str] = Field(min_length=1, max_length=200)
    disabled: bool


class ChannelSourceInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def validate_url(self):
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("直播地址必须是有效的 HTTP(S) URL")
        return self


class ChannelSourcesUpdate(BaseModel):
    """Replace the user-managed source list of an existing channel."""

    sources: list[ChannelSourceInput] = Field(min_length=1, max_length=20)
    primary_index: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_index(self):
        if self.primary_index >= len(self.sources):
            raise ValueError("首选源索引超出范围")
        keys = [url_key(row.url) for row in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("同一频道不能重复添加相同的直播源地址")
        return self


class ChannelCreate(BaseModel):
    channel_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    # ``m3u_url`` is the single-source legacy field; new clients send ``sources``.
    m3u_url: str | None = Field(default=None, max_length=2000)
    sources: list[ChannelSourceInput] = Field(default_factory=list, max_length=20)
    category: str = Field(default="地方与其他", max_length=40)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_sources(self):
        if not self.sources and self.m3u_url:
            self.sources = [ChannelSourceInput(url=self.m3u_url)]
        if not self.sources:
            raise ValueError("至少要填写一条直播源 URL")
        if self.m3u_url is None:
            self.m3u_url = self.sources[0].url
        else:
            parsed = urlparse(self.m3u_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("直播地址必须是有效的 HTTP(S) URL")
        keys: set[str] = set()
        for row in self.sources:
            key = url_key(row.url)
            if key in keys:
                raise ValueError("同一频道不能重复添加相同的直播源地址")
            keys.add(key)
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
            raise ValueError("地址必须是有效的 HTTP(S) URL")
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
                raise ValueError("地址必须是有效的 HTTP(S) URL")
        return self


class SourceTestRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class SourceActivation(BaseModel):
    mode: Literal["temporary", "preferred"] = "temporary"


class SourceReorder(BaseModel):
    channel_id: str = Field(min_length=1, max_length=100)
    source_ids: list[str] = Field(min_length=1, max_length=200)


class ChannelSourceSelection(BaseModel):
    source_id: str = Field(min_length=1, max_length=100)
    mode: Literal["temporary", "preferred"] = "temporary"


class SourceImportRequest(BaseModel):
    source: str = Field(min_length=1, max_length=2000)
    match: list[Literal["tvg_id", "name", "normalized_name"]] = Field(
        default_factory=lambda: ["tvg_id", "name", "normalized_name"]
    )
    dry_run: bool = True


class BufferProfileUpdate(BaseModel):
    profile: Literal["low", "balanced", "smooth"]


class PushEmailConfig(BaseModel):
    host: str = Field(default="", max_length=200)
    port: int = Field(default=465, ge=1, le=65535)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=500)
    sender: str = Field(default="", max_length=200)
    recipient: str = Field(default="", max_length=500)
    use_ssl: bool = True


class PushConfigUpdate(BaseModel):
    provider: Literal["none", "email"] = "none"
    email: PushEmailConfig = Field(default_factory=PushEmailConfig)


class RecordingEncodeUpdate(BaseModel):
    enabled: bool = True
    height: Literal[0, 480, 540, 720, 1080] = 720
    crf: int = Field(default=26, ge=18, le=34)
    preset: Literal["ultrafast", "veryfast", "faster", "fast", "medium", "slow"] = "veryfast"
    keep_original: bool = False


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
        raise HTTPException(status_code=400, detail="对象 ID 无效") from exc


logger = logging.getLogger(__name__)


@app.middleware("http")
async def api_authentication(request: Request, call_next):
    token = os.getenv("WEB_API_TOKEN", "")
    if token and request.url.path.startswith("/api/"):
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {token}"):
            return JSONResponse(status_code=401, content={"detail": "API 访问令牌无效"})
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
        raise HTTPException(status_code=404, detail="该频道当前无法观看直播")
    return channel


@app.get("/api/channels/admin")
def channels_admin() -> list[dict]:
    """Full channel list for the management page, including disabled and stream-less rows."""
    rows = [
        channel_display(row)
        for row in database.channels.find({}, {"stream_quality": 0}).sort("name", 1)
    ]
    counts = {
        item["_id"]: item["count"]
        for item in database.sources.aggregate(
            [{"$match": {"channel_id": {"$ne": None}}}, {"$group": {"_id": "$channel_id", "count": {"$sum": 1}}}]
        )
    }
    for row in rows:
        row["stream_count"] = counts.get(row["channel_id"], len(row.get("stream_candidates") or []))
        row["source_locked"] = bool(row.get("preferred_source_id"))
        row["active_state"] = row.get("active_state") or "primary"
        row["epg_matched"] = row.get("epg_source") == "xmltv"
        row.pop("stream_candidates", None)
        row.pop("raw_m3u", None)
    return serialize(rows)


@app.patch("/api/channels/{channel_id}")
def update_channel_admin(channel_id: str, payload: ChannelAdminUpdate) -> dict:
    current = database.channels.find_one({"channel_id": channel_id})
    if not current:
        raise HTTPException(status_code=404, detail="频道不存在")
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
    display["stream_count"] = database.sources.count_documents({"channel_id": channel_id})
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
            detail="频道 ID 已存在，并会在下次直播源同步时被覆盖",
        )
    document = payload.model_dump()
    document.pop("sources", None)
    document["source"] = "manual"
    document["created_at"] = datetime.now(UTC)
    document["updated_at"] = document["created_at"]
    document["stream_candidates"] = [row.url for row in payload.sources]
    document["source_policy"] = "locked_primary"
    database.channels.insert_one(document)
    primary = payload.sources[0]
    source = store.create_source(
        database,
        channel_id=payload.channel_id,
        url=primary.url,
        label=primary.name or payload.name,
        kind="custom",
        provider="手动添加",
        origin="custom",
        locked=True,
        priority=0,
    )
    store.set_preferred(database, payload.channel_id, source["source_id"])
    for index, row in enumerate(payload.sources[1:], start=1):
        if store.duplicate_url(database, payload.channel_id, row.url):
            continue
        store.create_source(
            database,
            channel_id=payload.channel_id,
            url=row.url,
            label=row.name or f"{payload.name} 备用 {index}",
            kind="custom",
            provider="手动添加",
            origin="custom",
            priority=index,
        )
    row = database.channels.find_one({"channel_id": payload.channel_id})
    display = channel_display(row)
    display["stream_count"] = database.sources.count_documents({"channel_id": payload.channel_id})
    return serialize(display)


@app.put("/api/channels/{channel_id}/sources")
def replace_channel_sources(channel_id: str, payload: ChannelSourcesUpdate) -> dict:
    """Replace the user-managed source list of a channel from the channel editor."""
    try:
        result = store.replace_user_sources(
            database,
            channel_id,
            [row.model_dump() for row in payload.sources],
            primary_index=payload.primary_index,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize({**result, **channel_stream_options(channel_id)})


@app.delete("/api/channels/{channel_id}", status_code=204, response_class=Response)
def delete_channel(channel_id: str) -> Response:
    current = database.channels.find_one({"channel_id": channel_id})
    if not current:
        raise HTTPException(status_code=404, detail="频道不存在")
    if current.get("source") == "iptv-api":
        raise HTTPException(
            status_code=409,
            detail="直播源同步的频道不可删除，只能禁用",
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
    database.sources.delete_many({"channel_id": channel_id})
    return Response(status_code=204)


@app.get("/api/channels/{channel_id}/detail")
def channel_detail(channel_id: str) -> dict:
    """Channel detail drawer: stream list, recent recordings, EPG match status."""
    channel = database.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
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


def _channel_name_map(channels: list[dict]) -> dict[str, str]:
    return {row["channel_id"]: (row.get("name_override") or row.get("name") or row["channel_id"]) for row in channels}


def _public_source(row: dict, channel_names: dict[str, str]) -> dict:
    channel_id = row.get("channel_id")
    return {
        **source_public(row),
        "name": row.get("label") or urlparse(row.get("url") or "").netloc,
        "source_type": "seed" if row.get("origin") == "seed" else "custom",
        "channel_name": channel_names.get(channel_id) if channel_id else None,
    }


def _source_group(row: dict) -> dict[str, str]:
    """Group a source under the "total source" it came from.

    The management page is two-level: a provider (official multicast bridge,
    IPTV-API sync, third-party M3U, user custom, M3U import) owns many channel
    sources.  Older rows have an empty ``provider``, so derive a stable label
    from kind/origin instead of showing an empty group.
    """
    provider = (row.get("provider") or "").strip()
    origin = row.get("origin") or "custom"
    kind = row.get("kind") or ""
    if provider:
        # The seed script writes the raw provider id; show its friendly name and
        # keep every IPTV-API batch in one group.
        key, label = ("iptv-api", "IPTV-API 同步") if provider.lower() == "iptv-api" else (provider, provider)
    elif origin == "seed":
        key, label = "iptv-api", "IPTV-API 同步"
    elif kind == "udpxy":
        key, label = "bridge", "官方组播桥接"
    elif origin == "import":
        key, label = "import", "M3U 导入"
    elif origin == "migration":
        key, label = "legacy-m3u", "历史 M3U 源"
    else:
        key, label = origin, "用户自定义"
    return {"key": key, "label": label, "origin": origin}


@app.get("/api/sources")
def sources() -> dict:
    """Channel-grouped source list for the management page."""
    channels = list(
        database.channels.find(
            {},
            {
                "channel_id": 1,
                "name": 1,
                "name_override": 1,
                "m3u_url": 1,
                "preferred_source_id": 1,
                "active_source_id": 1,
                "source_policy": 1,
            },
        ).sort("channel_id", 1)
    )
    channel_names = _channel_name_map(channels)
    rows: list[dict] = []
    for channel in channels:
        payload = store.payload_for_channel(database, channel)
        for row in payload["sources"]:
            rows.append(
                {
                    **_public_source(row, channel_names),
                    "name": row.get("label") or urlparse(row.get("url") or "").netloc,
                    "channel_name": channel_names.get(channel["channel_id"]),
                    "current": row.get("current", False),
                    "preferred": row.get("preferred", False),
                    "policy": payload["policy"],
                    "group": _source_group(row),
                }
            )
    unbound = [
        {**_public_source(row, channel_names), "current": False, "preferred": False, "group": _source_group(row)}
        for row in store.unbound_sources(database)
    ]
    groups: dict[str, dict] = {}
    for row in rows:
        group = row["group"]
        entry = groups.setdefault(
            group["key"],
            {"key": group["key"], "label": group["label"], "origin": group["origin"], "source_count": 0, "channel_ids": set()},
        )
        entry["source_count"] += 1
        entry["channel_ids"].add(row.get("channel_id"))
    group_rows = [
        {**entry, "channel_count": len(entry["channel_ids"]), "channel_ids": sorted(entry["channel_ids"])}
        for entry in groups.values()
    ]
    group_rows.sort(key=lambda item: (-item["source_count"], item["label"]))
    latest_sync = database.channels.find_one(sort=[("updated_at", -1)])
    return serialize(
        {
            "m3u_summary": {
                "source": "M3U 同步 + 用户自定义",
                "last_sync_at": (latest_sync or {}).get("updated_at"),
                "seed_stream_count": sum(1 for row in rows if row.get("origin") == "seed"),
            },
            "groups": group_rows,
            "sources": rows + unbound,
            "unbound": unbound,
        }
    )


@app.post("/api/sources", status_code=201)
def create_source(payload: SourceCreate) -> dict:
    existing = store.duplicate_url(database, payload.channel_id, payload.url)
    if existing:
        raise HTTPException(status_code=409, detail="该频道已存在相同地址的直播源")
    document = store.create_source(
        database,
        channel_id=payload.channel_id,
        url=payload.url,
        label=payload.name,
        note=payload.note,
        enabled=payload.enabled,
        origin="custom",
    )
    return serialize(source_public(document))


@app.patch("/api/sources/{source_id}")
def update_source(source_id: str, payload: SourceUpdate) -> dict:
    update = payload.model_dump(exclude_unset=True)
    if not update:
        raise HTTPException(status_code=422, detail="没有提供可更新的直播源字段")
    if update.get("name") is not None:
        update["label"] = update.pop("name")
    channel_id = update.get("channel_id", "__unchanged__")
    try:
        if channel_id is not None and channel_id != "__unchanged__":
            result = store.bind_source(database, source_id, channel_id)
        else:
            update.pop("channel_id", None)
            result = store.update_source(database, source_id, update)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize(source_public(result))


@app.delete("/api/sources/{source_id}", status_code=204, response_class=Response)
def delete_source(source_id: str) -> Response:
    try:
        deleted = store.delete_source(database, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="直播源不存在")
    return Response(status_code=204)


@app.post("/api/sources/{source_id}/preferred")
def prefer_source(source_id: str) -> dict:
    row = database.sources.find_one({"source_id": source_id})
    if not row or not row.get("channel_id"):
        raise HTTPException(status_code=404, detail="直播源未绑定频道")
    try:
        channel = store.set_preferred(database, row["channel_id"], source_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize(store.payload_for_channel(database, channel))


@app.post("/api/sources/{source_id}/activate")
def activate_source(source_id: str, payload: SourceActivation) -> dict:
    row = database.sources.find_one({"source_id": source_id})
    if not row or not row.get("channel_id"):
        raise HTTPException(status_code=404, detail="直播源未绑定频道")
    try:
        channel = store.activate(
            database,
            row["channel_id"],
            source_id,
            temporary=payload.mode == "temporary",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize(store.payload_for_channel(database, channel))


@app.post("/api/sources/reorder")
def reorder_sources(payload: SourceReorder) -> dict:
    store.reorder(database, payload.channel_id, payload.source_ids)
    channel = database.channels.find_one({"channel_id": payload.channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
    return serialize(store.payload_for_channel(database, channel))


@app.post("/api/sources/import")
def import_sources(payload: SourceImportRequest) -> dict:
    """Import an M3U as backup sources; never replaces a channel's primary."""
    match = tuple(payload.match)
    try:
        if payload.dry_run:
            return serialize(preview_import(database, source=payload.source, match=match))
        return serialize(apply_import(database, source=payload.source, match=match))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail=f"无法读取 M3U：{exc}") from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"无法读取 M3U：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _probe_source(url: str) -> dict:
    """Probe an HLS source: fetch playlist + first media segment, return status/size/elapsed."""
    if live_http_client is None:
        raise HTTPException(status_code=503, detail="直播代理尚未就绪")
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
    channel = database.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
    payload = store.payload_for_channel(database, channel)
    # Legacy fields kept for clients that still render an index-based picker.
    candidates = [row.get("url") for row in payload["sources"]]
    active = payload.get("active_source_id")
    payload["candidates"] = [
        {
            "index": index,
            "source_id": row.get("source_id"),
            "host": urlparse(row.get("url") or "").netloc,
            "current": row.get("source_id") == active,
            "quality": row.get("quality"),
            "kind": row.get("kind"),
            "locked": row.get("locked"),
            "enabled": row.get("enabled"),
            "url": row.get("url"),
        }
        for index, row in enumerate(payload["sources"])
    ]
    payload["current_index"] = next(
        (index for index, row in enumerate(payload["sources"]) if row.get("source_id") == active),
        None,
    )
    payload["manual"] = {"policy": payload["policy"]} if payload["policy"] == "manual" else None
    payload["candidates_urls"] = candidates
    return serialize(payload)


@app.get("/api/channels/{channel_id}/streams")
def channel_streams(channel_id: str) -> dict:
    return channel_stream_options(channel_id)


@app.post("/api/channels/{channel_id}/source")
def select_channel_source(channel_id: str, payload: ChannelSourceSelection) -> dict:
    channel = database.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
    try:
        updated = store.activate(
            database, channel_id, payload.source_id, temporary=payload.mode == "temporary"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize(store.payload_for_channel(database, updated))


@app.post("/api/channels/{channel_id}/stream")
def select_channel_stream(channel_id: str, payload: StreamSelection) -> dict:
    """Legacy index-based picker; translates to a source_id selection."""
    channel = database.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
    payload_sources = store.payload_for_channel(database, channel)["sources"]
    if payload.index is None:
        return serialize(store.payload_for_channel(database, channel))
    if payload.index < 0 or payload.index >= len(payload_sources):
        raise HTTPException(status_code=400, detail="直播线路序号超出范围")
    store.activate(
        database,
        channel_id,
        payload_sources[payload.index]["source_id"],
        temporary=True,
    )
    return channel_stream_options(channel_id)


async def _probe_live_url(url: str) -> None:
    """Raise when an upstream cannot serve a usable live stream."""
    if live_http_client is None:
        raise LiveProxyError("直播代理尚未就绪")
    upstream = await open_live_response(live_http_client, url)
    if not response_is_playlist(upstream):
        await upstream.aclose()
        return
    content = await read_live_response(upstream)
    if not content.lstrip().startswith(b"#EXTM3U"):
        raise LiveProxyError("上游直播源返回了无效的 HLS 播放列表")


async def live_response(url: str) -> Response:
    if live_http_client is None:
        raise HTTPException(status_code=503, detail="直播代理尚未就绪")
    try:
        upstream = await open_live_response(live_http_client, url)
    except LiveProxyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return await build_live_response(upstream)


async def build_live_response(upstream) -> Response:
    """Proxy an already-opened upstream into a browser-safe response."""
    media_type = live_media_type(upstream)
    if response_is_playlist(upstream):
        try:
            content = await read_live_response(upstream)
        except LiveProxyError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not content.lstrip().startswith(b"#EXTM3U"):
            raise HTTPException(status_code=502, detail="上游直播源返回了无效的 HLS 播放列表")
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

    rows = resolver.sources_for(database, channel)
    if not rows:
        # Legacy channel without migrated sources: behave exactly as before.
        return await live_response(channel["m3u_url"])

    candidates = resolver.candidates(database, channel)
    if not candidates:
        raise HTTPException(status_code=502, detail="该频道没有启用的直播源")

    preferred_id = (
        channel.get("preferred_source_id")
        or channel.get("active_source_id")
        or (candidates[0].get("source_id") if candidates else None)
    )
    if channel.get("active_state") == "fallback" and resolver.retry_due(channel):
        resolver.mark_retry_probe(database, channel)

    last_error: Exception | None = None
    for source in candidates:
        try:
            upstream = await open_live_response(live_http_client, source["url"])
        except LiveProxyError as exc:
            last_error = exc
            resolver.record_failure(database, source, str(exc))
            logger.warning(
                "live source unavailable channel=%s source=%s error=%s",
                channel_id,
                source.get("source_id"),
                exc,
            )
            continue
        try:
            response = await build_live_response(upstream)
        except HTTPException as exc:
            last_error = exc
            resolver.record_failure(database, source, str(exc.detail))
            logger.warning(
                "live source rejected channel=%s source=%s detail=%s",
                channel_id,
                source.get("source_id"),
                exc.detail,
            )
            continue
        resolver.record_success(database, source)
        is_preferred = source.get("source_id") == preferred_id
        if not is_preferred:
            resolver.mark_active(database, channel, source, "fallback")
        elif channel.get("active_source_id") != source.get("source_id") or channel.get("active_state") != "primary":
            resolver.mark_active(database, channel, source, "primary")
        response.headers["X-IPTV-Source-Id"] = source.get("source_id") or ""
        response.headers["X-IPTV-Source-State"] = "primary" if is_preferred else "fallback"
        return response

    detail = str(last_error) if last_error else "所有直播源均不可用"
    raise HTTPException(status_code=502, detail=detail)


@app.get("/api/channels/{channel_id}/normalized/{filename}")
def normalized_live_segment(channel_id: str, filename: str) -> Response:
    if channel_id not in normalized_channels or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="规范化直播分片不存在")
    if not filename.endswith(".ts"):
        raise HTTPException(status_code=404, detail="规范化直播分片不存在")
    path = channel_cache_directory(normalized_live_root, channel_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="规范化直播分片不存在")
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
    return serialize(list(database.summaries.find({"ready_for_push": {"$ne": False}, "outdated": {"$ne": True}}, {"embedding": 0}).sort("created_at", -1).limit(limit)))


@app.get("/api/summaries")
def summaries(target_date: date = Query(alias="date")) -> list[dict]:
    start, end = local_day_window(target_date)
    return serialize(
        list(database.summaries.find({"created_at": {"$gte": start, "$lt": end}, "ready_for_push": {"$ne": False}, "outdated": {"$ne": True}}, {"embedding": 0}))
    )


@app.get("/api/summaries/{summary_id}")
def summary_detail(summary_id: str) -> dict:
    summary = database.summaries.find_one({"_id": object_id(summary_id), "ready_for_push": {"$ne": False}, "outdated": {"$ne": True}}, {"embedding": 0})
    if not summary:
        raise HTTPException(status_code=404, detail="节目摘要不存在")
    transcript = database.transcripts.find_one({"_id": summary.get("transcript_id")}, {"embedding": 0})
    return serialize({**summary, "transcript": transcript})


@app.get("/api/search")
def search(q: str = Query(min_length=1), vector: bool = False) -> dict:
    summary_rows = list(database.summaries.find({"$text": {"$search": q}, "ready_for_push": {"$ne": False}, "outdated": {"$ne": True}}, {"embedding": 0}).limit(30))
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
            from iptv_pilot.common.embedding import embed_text

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
        raise HTTPException(status_code=422, detail="所选频道不是已启用的直播频道")


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
        raise HTTPException(status_code=404, detail="订阅不存在")
    update = payload.model_dump(exclude_unset=True)
    if not update:
        raise HTTPException(status_code=422, detail="没有提供可更新的订阅字段")
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
        raise HTTPException(status_code=404, detail="订阅不存在")
    return Response(status_code=204)


@app.get("/api/recordings")
def recordings(status: str | None = None, limit: int = Query(100, ge=1, le=500)) -> list[dict]:
    query = {"status": status} if status else {}
    return serialize(list(database.recordings.find(query).sort("start_time", -1).limit(limit)))


@app.post("/api/recordings/manual", status_code=201)
def create_manual_recording(payload: ManualRecordingCreate) -> dict:
    ensure_enabled_channel(payload.channel_id)
    now = datetime.now(UTC)
    encode_setting = recording_encoding.load_settings(database)
    document = {
        "subscription_id": None,
        "channel_id": payload.channel_id,
        "program_title": payload.program_title,
        "start_time": now,
        "end_time": now + timedelta(seconds=payload.duration_sec),
        "scheduled_start": now,
        "scheduled_end": now + timedelta(seconds=payload.duration_sec),
        "window_seconds": payload.duration_sec,
        "status": "queued",
        "retry_count": 0,
        "asr_enabled": payload.asr_enabled,
        "live_asr_enabled": payload.live_asr_enabled,
        "asr_model": payload.asr_model,
        "llm_enabled": payload.llm_enabled,
        "push_enabled": payload.push_enabled,
        "auto_bookmark": False,
        "manual": True,
        "created_at": now,
        "encode_status": "pending" if encode_setting["enabled"] else "disabled",
        "encode_retry_count": 0,
        "next_encode_attempt_at": None,
    }
    result = database.recordings.insert_one(document)
    return serialize(database.recordings.find_one({"_id": result.inserted_id}))


@app.get("/api/recordings/{recording_id}")
def recording_detail(recording_id: str) -> dict:
    recording = database.recordings.find_one({"_id": object_id(recording_id)})
    if not recording:
        raise HTTPException(status_code=404, detail="录制任务不存在")
    transcript = database.transcripts.find_one(
        {"recording_id": recording["_id"]}, {"embedding": 0, "segments": 0}
    )
    summary = database.summaries.find_one(
        {"recording_id": recording["_id"], "ready_for_push": {"$ne": False}, "outdated": {"$ne": True}}, {"embedding": 0}
    )
    events = []
    if summary:
        events = list(database.push_events.find({"summary_id": summary["_id"]}))
    from iptv_pilot.common.live import view
    from iptv_pilot.recorder.partials import coverage

    return serialize(
        {
            "recording": recording,
            "transcript": transcript,
            "summary": summary,
            "push_events": events,
            "live": view(database, recording),
            "coverage": coverage(recording) if recording.get("recording_parts") else None,
        }
    )


class TranscriptCorrection(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(ge=0)
    notify: bool = False


@app.post("/api/recordings/{recording_id}/analysis/regenerate")
def regenerate_analysis(recording_id: str) -> dict:
    from iptv_pilot.llm.corrections import EditConflict, regenerate

    row = database.recordings.find_one({"_id": object_id(recording_id)})
    if not row:
        raise HTTPException(404, "录制任务不存在")
    try:
        return regenerate(database, row)
    except EditConflict as exc:
        raise HTTPException(409, "分析尚未完成，请稍后重试") from exc
    except KeyError as exc:
        raise HTTPException(404, "转写尚未完成") from exc


@app.get("/api/recordings/{recording_id}/transcript")
def transcript_segments(recording_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100), focus: str | None = None, q: str = Query("", max_length=200)) -> dict:
    from iptv_pilot.llm.traceable import identify

    rid = object_id(recording_id)
    row = database.recordings.find_one({"_id": rid})
    transcript = database.transcripts.find_one({"recording_id": rid}, {"embedding": 0})
    if not row or not transcript:
        raise HTTPException(404, "转写尚未完成或不存在")
    from iptv_pilot.common.transcript import search_segments

    segments = search_segments(identify(transcript.get("segments", [])), q)
    if focus:
        index = next((i for i, s in enumerate(segments) if s["id"] == focus), None)
        if index is not None:
            offset = (index//limit)*limit
    return serialize({"segments": segments[offset:offset+limit], "offset": offset, "total": len(segments),
                      "revision": transcript.get("revision", 0),
                      "editable": row.get("status") in {"asr_done", "summarized", "summary_failed"} and row.get("summary_state") != "finalizing"})


@app.get("/api/recordings/{recording_id}/transcript/subtitles")
def export_subtitles(recording_id: str, format: Literal["srt", "vtt"] = "srt", part_id: str | None = None):
    from iptv_pilot.common.transcript import subtitles
    from iptv_pilot.llm.traceable import identify

    rid = object_id(recording_id)
    transcript = database.transcripts.find_one({"recording_id": rid})
    if not transcript:
        raise HTTPException(404, "转写尚未完成或不存在")
    segments = identify(transcript.get("segments", []))
    if part_id and not any(s["part_id"] == part_id for s in segments):
        raise HTTPException(404, "片段转写不存在")
    try:
        body = subtitles(segments, format, part_id)
    except ValueError as exc:
        raise HTTPException(409, "断流录像请按片段导出字幕，以保证时间同步") from exc
    suffix = "-part" if part_id else ""
    return Response(body, media_type="text/vtt" if format == "vtt" else "application/x-subrip",
                    headers={"Content-Disposition": f'attachment; filename="{rid}{suffix}.{format}"'})


@app.patch("/api/recordings/{recording_id}/transcript/segments/{segment_id}")
def correct_transcript(recording_id: str, segment_id: str, payload: TranscriptCorrection) -> dict:
    from iptv_pilot.llm.corrections import EditConflict, correct

    row = database.recordings.find_one({"_id": object_id(recording_id)})
    if not row:
        raise HTTPException(404, "录制任务不存在")
    try:
        return correct(database, row, segment_id, payload.text, payload.expected_revision, payload.notify)
    except EditConflict as exc:
        raise HTTPException(409, "文本版本或处理状态已变化，请刷新后重试") from exc
    except KeyError as exc:
        raise HTTPException(404, "转写段落不存在") from exc


@app.get("/api/recordings/{recording_id}/transcript/edits")
def transcript_edits(recording_id: str) -> list[dict]:
    return serialize(list(database.transcript_edits.find({"recording_id": object_id(recording_id)},
        {"old_summary": 0}).sort("created_at", -1).limit(50)))


@app.get("/api/recordings/{recording_id}/partial-transcripts")
def partial_transcripts(recording_id: str) -> list[dict]:
    from iptv_pilot.common.live import merge_chunks, segments_text

    rid = object_id(recording_id)
    row = database.recordings.find_one({"_id": rid})
    if not row:
        raise HTTPException(404, "录制任务不存在")
    result, seen = [], set()
    for part in row.get("recording_parts", []):
        epoch = part.get("epoch")
        if not epoch or epoch in seen:
            continue
        seen.add(epoch)
        chunks = list(database.live_chunks.find({"recording_id": rid, "epoch": epoch,
            "lane": "draft", "state": "done"}, {"segments": 1}).sort("seq", 1))
        text = segments_text(merge_chunks(chunks))
        if text:
            result.append({"part_id": part["id"], "offset": part["offset"], "text": text, "draft": True})
    return result


def sealed_part(recording_id, part_id):
    row = database.recordings.find_one({"_id": object_id(recording_id)})
    part = next((p for p in (row or {}).get("recording_parts", []) if p["id"] == part_id and p.get("sealed")), None)
    if not part:
        raise HTTPException(404, "已封口的录制片段不存在")
    return {**row, "file_path": part["path"], "status": "completed"}, part


@app.get("/api/recordings/{recording_id}/parts/{part_id}/play")
def play_part(recording_id: str, part_id: str, background_tasks: BackgroundTasks):
    row, part = sealed_part(recording_id, part_id)
    return playable_response(recording_id+"-"+part_id, row, background_tasks)


@app.get("/api/recordings/{recording_id}/parts/{part_id}/download")
def download_part(recording_id: str, part_id: str, background_tasks: BackgroundTasks):
    row, part = sealed_part(recording_id, part_id)
    return download_response(recording_id+"-"+part_id, row, background_tasks)


def media_recording(recording_id: str) -> dict:
    recording = database.recordings.find_one({"_id": object_id(recording_id)})
    if not recording:
        raise HTTPException(status_code=404, detail="录制任务不存在")
    if recording.get("status") in {"queued", "recording"}:
        raise HTTPException(status_code=409, detail="录制任务尚未完成")
    return recording


@app.get("/api/recordings/{recording_id}/play", response_class=FileResponse)
def play_recording(recording_id: str, background_tasks: BackgroundTasks) -> FileResponse:
    recording = media_recording(recording_id)
    return playable_response(recording_id, recording, background_tasks)


def playable_response(recording_id, recording, background_tasks):
    reader = None
    try:
        source = resolve_recording_file(recording, Path(os.getenv("RECORDINGS_ROOT", "/data/recordings")))
        reader = MediaReader(database, source)
        reader.__enter__()
        asset = prepare_browser_media(
            recording_id,
            recording,
            Path(os.getenv("RECORDINGS_ROOT", "/data/recordings")),
            Path(os.getenv("MEDIA_CACHE_DIR", "/data/media-cache")),
            maximum_cache_bytes=int(os.getenv("MEDIA_CACHE_MAX_BYTES", str(20 * 1024**3))),
            timeout_seconds=int(os.getenv("MEDIA_REMUX_TIMEOUT_SECONDS", "300")),
        )
    except MediaNotFoundError as exc:
        if reader:
            reader.close()
        raise HTTPException(status_code=404, detail="录制媒体不可用") from exc
    except MediaPathError as exc:
        if reader:
            reader.close()
        raise HTTPException(status_code=404, detail="录制媒体不可用") from exc
    except MediaPreparationError as exc:
        if reader:
            reader.close()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MediaBusy as exc:
        raise HTTPException(409, "媒体文件已更新，请重新打开播放") from exc
    except Exception:
        if reader:
            reader.close()
        raise
    if asset.path == source:
        background_tasks.add_task(reader.close)
    else:
        reader.close()
    return FileResponse(
        asset.path,
        media_type=asset.media_type,
        filename=f"{recording_id}.mp4",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
        background=background_tasks,
    )


@app.get("/api/recordings/{recording_id}/download", response_class=FileResponse)
def download_recording(recording_id: str, background_tasks: BackgroundTasks) -> FileResponse:
    recording = media_recording(recording_id)
    return download_response(recording_id, recording, background_tasks)


def download_response(recording_id, recording, background_tasks):
    try:
        source = resolve_recording_file(
            recording, Path(os.getenv("RECORDINGS_ROOT", "/data/recordings"))
        )
    except (MediaNotFoundError, MediaPathError) as exc:
        raise HTTPException(status_code=404, detail="录制媒体不可用") from exc
    suffix = source.suffix.lower() or ".bin"
    reader = MediaReader(database, source)
    try:
        reader.__enter__()
    except MediaBusy as exc:
        raise HTTPException(409, "媒体文件已更新，请重新下载") from exc
    background_tasks.add_task(reader.close)
    return FileResponse(
        source,
        media_type=download_media_type(source),
        filename=f"{recording_id}{suffix}",
        content_disposition_type="attachment",
        background=background_tasks,
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


@app.get("/api/settings/push")
def get_push_config() -> dict:
    return serialize(push_settings.public_config(database))


@app.patch("/api/settings/push")
def set_push_config(payload: PushConfigUpdate) -> dict:
    try:
        return serialize(
            push_settings.save_config(
                database,
                payload.provider,
                payload.email.model_dump(),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/settings/push/test")
def test_push_config() -> dict:
    config = push_settings.load_config(database)
    if config["provider"] != "email":
        raise HTTPException(status_code=422, detail="当前推送渠道不是邮箱")
    provider = EmailProvider(config["email"])
    try:
        provider.push_text(
            "IPTVPilot 测试邮件\n\n如果你收到这封邮件，说明推送配置可用。"
        )
    except Exception as exc:  # delivery errors are reported to the UI verbatim
        raise HTTPException(status_code=502, detail=f"发送失败：{exc}") from exc
    return {"ok": True, "recipient": config["email"].get("recipient", "")}


@app.get("/api/settings/recording")
def get_recording_encoding() -> dict:
    return serialize(recording_encoding.load_settings(database))


@app.patch("/api/settings/recording")
def set_recording_encoding(payload: RecordingEncodeUpdate) -> dict:
    return serialize(recording_encoding.save_settings(database, payload.model_dump()))


@app.get("/api/system/status")
def system_status() -> dict:
    from iptv_pilot.common.runtime import pipeline_status

    running = list(
        database.recordings.find(
            {"status": "recording"},
            {
                "channel_id": 1,
                "program_title": 1,
                "claimed_by": 1,
                "claim_id": 1,
                "recording_started_at": 1,
                "heartbeat_at": 1,
                "source_id": 1,
            },
        )
    )
    now = datetime.now(UTC)
    epg_upcoming = database.epg_programs.count_documents(
        {"start_time": {"$gte": now, "$lte": now + timedelta(hours=24)}}
    )
    epg_latest = database.epg_programs.find_one(sort=[("start_time", -1)]) or {}
    return {
        "pipeline": serialize(pipeline_status(database, now)),
        "channels": database.channels.count_documents({"enabled": True, "disabled": {"$ne": True}, "m3u_url": {"$exists": True}}),
        "epg_programs": database.epg_programs.count_documents({}),
        "epg_upcoming_24h": epg_upcoming,
        "epg_latest_start": serialize(epg_latest.get("start_time")),
        "epg_status": "ok" if epg_upcoming else "stale",
        "subscriptions": database.subscriptions.count_documents({"enabled": True}),
        "recordings_by_status": {
            row["_id"]: row["count"]
            for row in database.recordings.aggregate(
                [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
            )
        },
        "recordings_running": len(running),
        "recordings_imminent": database.recordings.count_documents({
            "status": "queued", "scheduled_start": {"$lte": now + timedelta(minutes=5)},
            "scheduled_end": {"$gte": now},
        }),
        "recordings_queued": database.recordings.count_documents(
            {"status": "queued", "scheduled_start": {"$lte": now}}
        ),
        "recording_workers": [
            {
                "worker_id": recording.get("claimed_by"),
                "claim_id": recording.get("claim_id"),
                "recording_id": str(recording["_id"]),
                "channel_id": recording.get("channel_id"),
                "program_title": recording.get("program_title"),
                "source_id": recording.get("source_id"),
                "started_at": serialize(recording.get("recording_started_at")),
                "heartbeat_at": serialize(recording.get("heartbeat_at")),
            }
            for recording in running
        ],
        "latest_summary_at": serialize(
            (database.summaries.find_one(sort=[("created_at", -1)]) or {}).get("created_at")
        ),
        "normalizer_pool": _normalizer_pool(),
    }


class MaintenanceUpdate(BaseModel):
    services: list[str]
    draining: bool


@app.get("/api/system/maintenance")
def get_maintenance() -> dict:
    from iptv_pilot.common.runtime import maintenance_status

    return maintenance_status(database)


@app.post("/api/system/maintenance")
def set_maintenance(payload: MaintenanceUpdate) -> dict:
    from iptv_pilot.common.runtime import maintenance_status

    allowed = {"recorder", "encoder", "asr", "llm", "push", "web", "background",
               "scheduler", "epg-engine", "stream-quality", "channel-sync"}
    if not payload.services or not set(payload.services) <= allowed:
        raise HTTPException(status_code=422, detail="不支持的维护服务")
    for name in payload.services:
        database.settings.update_one({"key": f"drain:{name}"},
                                     {"$set": {"value": payload.draining}}, upsert=True)
    return maintenance_status(database)


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
        raise HTTPException(status_code=404, detail="收藏不存在")
    return Response(status_code=204)


def main() -> None:
    uvicorn.run(
        "iptv_pilot.web.main:app",
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
