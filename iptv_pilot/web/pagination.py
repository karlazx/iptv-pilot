"""Stable bounded database pages; cursors are tied to their filter and sort."""

import base64
import hashlib
import json
import re
from datetime import datetime

from bson import ObjectId, json_util
from fastapi import HTTPException

SPECS = {
    "recordings": ("recordings", "start_time", ["program_title", "channel_id"]),
    "summaries": ("summaries", "created_at", ["program_title", "summary_text", "channel_id"]),
    "subscriptions": ("subscriptions", "_id", ["name", "keyword", "channel_id"]),
    "channels": ("channels", "_id", ["name", "name_override", "channel_id", "category", "category_override"]),
    "sources": ("sources", "_id", ["label", "channel_id", "url", "note"]),
    "bookmarks": ("bookmarks", "_id", ["note"]),
    "push-events": ("push_events", "_id", ["provider", "status", "error"]),
}


def page(database, kind, *, limit=50, cursor=None, q="", channel=None, status=None, first=None, last=None, order="desc"):
    if kind not in SPECS:
        raise HTTPException(404, "未知列表")
    collection, field, search = SPECS[kind]
    query = {}
    if q.strip():
        pattern = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{key: pattern} for key in search]
        if kind == "sources":
            query["$or"] += [{"saved_channel."+key: pattern} for key in ("name", "name_override")]
    if channel:
        query["channel_id"] = None if channel == "__unbound__" and kind == "sources" else channel
    if status:
        if kind in {"subscriptions", "sources", "channels"}:
            if status not in {"enabled", "disabled"}:
                raise HTTPException(422, "无效订阅状态")
            if kind == "channels":
                query["disabled"] = True if status == "disabled" else {"$ne": True}
            else:
                query["enabled"] = status == "enabled"
        else:
            query["status"] = status
    if first or last:
        timefield = "start_time" if kind == "recordings" else "created_at" if kind != "push-events" else "updated_at"
        query[timefield] = {**({"$gte": first} if first else {}), **({"$lt": last} if last else {})}
    if kind == "summaries":
        query.update(ready_for_push={"$ne": False}, outdated={"$ne": True})
    if kind == "bookmarks":
        query["user"] = "default"
        if q.strip():
            pattern = {"$regex": re.escape(q.strip()), "$options": "i"}
            query["$or"] += [{"item_type": typ, f"saved_{typ}.{key}": pattern} for typ in ("recording", "summary")
                              for key in ("program_title", "channel_id")]
    scope = hashlib.sha256(json.dumps([kind, query, order], default=str, sort_keys=True).encode()).hexdigest()[:24]
    direction = -1 if order == "desc" else 1
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError()
            data = json_util.loads(base64.urlsafe_b64decode(cursor+"="*(-len(cursor)%4)))
            if data["scope"] != scope or not isinstance(data["id"], ObjectId):
                raise ValueError()
            value = data["value"]
            if field != "_id" and value is not None and not isinstance(value, datetime):
                raise ValueError()
            op = "$lt" if direction == -1 else "$gt"
            if field == "_id":
                after = {"_id": {op: data["id"]}}
            else:
                cases = [{field: value, "_id": {op: data["id"]}}]
                if value is not None:
                    cases.append({field: {op: value}})
                    if direction == -1:
                        cases.append({field: None})
                elif direction == 1:
                    cases.append({field: {"$ne": None}})
                after = {"$or": cases}
            query = {"$and": [query, after]}
        except Exception as exc:
            raise HTTPException(400, "分页标记无效或筛选条件已变化，请从第一页开始") from exc
    projection = {"embedding": 0, "segments": 0, "stream_candidates": 0, "stream_quality": 0,
                  "raw_m3u": 0, "live_preview": 0, "recording_parts": 0}
    if kind == "channels":
        projection.pop("raw_m3u")  # Derive display category before stripping raw data.
    sort = [(field, direction)] + ([("_id", direction)] if field != "_id" else [])
    if kind in {"bookmarks", "sources"} and q.strip():
        pipeline = []
        if kind == "bookmarks":
            pipeline.append({"$match": {"user": "default"}})
            for typ, coll in (("recording", "recordings"), ("summary", "summaries")):
                pipeline.append({"$lookup": {"from": coll, "localField": "item_id", "foreignField": "_id",
                    "pipeline": [{"$project": {"program_title": 1, "channel_id": 1}}], "as": "saved_"+typ}})
        else:
            pipeline.append({"$lookup": {"from": "channels", "localField": "channel_id", "foreignField": "channel_id",
                "pipeline": [{"$project": {"name": 1, "name_override": 1}}], "as": "saved_channel"}})
        rows = list(database[collection].aggregate(pipeline+[
            {"$match": query}, {"$sort": dict(sort)}, {"$limit": limit+1},
            {"$project": {"saved_recording": 0, "saved_summary": 0, "saved_channel": 0}}]))
    else:
        rows = list(database[collection].find(query, projection).sort(sort).limit(limit+1))
    more, rows = len(rows) > limit, rows[:limit]
    following = None
    if more and rows:
        row = rows[-1]
        following = base64.urlsafe_b64encode(json_util.dumps({"scope": scope, "value": row.get(field), "id": row["_id"]}).encode()).decode().rstrip("=")
    return {"items": rows, "next_cursor": following, "has_more": more}


def setup(database):
    database.recordings.create_index([("start_time", -1), ("_id", -1)])
    database.recordings.create_index([("status", 1), ("start_time", -1), ("_id", -1)])
    database.recordings.create_index([("channel_id", 1), ("start_time", -1), ("_id", -1)])
    database.summaries.create_index([("created_at", -1), ("_id", -1)])
