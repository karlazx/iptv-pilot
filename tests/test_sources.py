"""Tests for the multi-source model and resolution rules."""

from datetime import UTC, datetime, timedelta

from iptv_pilot.sources import resolver, store
from iptv_pilot.sources.importing import preview_import
from iptv_pilot.sources.m3u import (
    M3UEntry,
    match_entries,
    normalize_channel_name,
    parse_m3u_text,
)
from iptv_pilot.sources.models import (
    make_source,
    mirror_candidates,
    ordered_sources,
    policy_for_channel,
    resolve_active,
    resolve_preferred,
    url_key,
)


def source(url, *, source_id, channel_id="C1", priority=0, locked=False, enabled=True, origin="custom"):
    row = make_source(
        channel_id=channel_id,
        url=url,
        label=url,
        origin=origin,
        priority=priority,
        locked=locked,
        enabled=enabled,
    )
    row["source_id"] = source_id
    return row


def channel(**overrides):
    base = {
        "channel_id": "C1",
        "preferred_source_id": "s-primary",
        "active_source_id": "s-primary",
        "source_policy": "locked_primary",
    }
    base.update(overrides)
    return base


def test_url_key_ignores_case_and_trailing_slash():
    assert url_key("HTTP://Example.COM/Live/") == url_key("http://example.com/Live")
    assert url_key("http://example.com/a?x=1") != url_key("http://example.com/a?x=2")


def test_ordered_sources_prefers_low_priority_and_enabled_only():
    rows = [
        source("http://b", source_id="b", priority=2),
        source("http://a", source_id="a", priority=1),
        source("http://off", source_id="off", priority=0, enabled=False),
    ]
    assert [row["source_id"] for row in ordered_sources(rows)] == ["a", "b"]


def test_resolve_preferred_uses_explicit_pointer_then_locked():
    primary = source("http://primary", source_id="s-primary", locked=True)
    backup = source("http://backup", source_id="s-backup", priority=1)
    assert resolve_preferred([backup, primary], channel())["source_id"] == "s-primary"
    assert resolve_preferred([backup, primary], channel(preferred_source_id=None))["source_id"] == "s-primary"
    # A disabled preferred pointer falls back to the locked source.
    disabled = source("http://primary", source_id="s-primary", locked=True, enabled=False)
    assert resolve_preferred([backup, disabled], channel())["source_id"] == "s-backup"


def test_resolve_active_keeps_fallback_until_changed():
    primary = source("http://primary", source_id="s-primary", locked=True)
    backup = source("http://backup", source_id="s-backup", priority=1)
    rows = [primary, backup]
    fallback = channel(active_source_id="s-backup", active_state="fallback")
    assert resolve_active(rows, fallback)["source_id"] == "s-backup"


def test_manual_policy_never_falls_back():
    primary = source("http://primary", source_id="s-primary", locked=True)
    backup = source("http://backup", source_id="s-backup", priority=1)
    manual = channel(source_policy="manual", active_source_id="s-backup")
    assert resolve_active([primary, backup], manual)["source_id"] == "s-primary"


def test_legacy_stream_manual_maps_to_manual_policy():
    assert policy_for_channel({"stream_manual": {"index": 1}}) == "manual"
    assert policy_for_channel({}) == "locked_primary"


def test_mirror_candidates_deduplicates_and_orders():
    rows = [
        source("http://second", source_id="b", priority=1),
        source("http://first", source_id="a", priority=0),
        source("http://first", source_id="c", priority=2),
    ]
    assert mirror_candidates(rows) == ["http://first", "http://second"]


def test_normalize_channel_name_strips_number_and_quality():
    assert normalize_channel_name("41 CCTV-1HD") == "CCTV1"
    assert normalize_channel_name("上海新闻综合 高清") == "上海新闻综合"
    assert normalize_channel_name("CCTV-1") == normalize_channel_name("cctv1超高清")


def test_parse_m3u_text_skips_update_rows():
    text = """#EXTM3U
#EXTINF:-1 tvg-id="CCTV-1" group-title="更新",CCTV-1
#EXTINF:-1 tvg-id="CCTV-2" group-title="央视",42 CCTV-2HD
http://example.com/cctv2.m3u8
"""
    entries = parse_m3u_text(text)
    assert [entry.channel_id for entry in entries] == ["CCTV-2"]


def test_match_entries_uses_tvg_id_then_normalized_name():
    channels = [
        {"channel_id": "CCTV-1", "name": "CCTV-1"},
        {"channel_id": "东方卫视", "name": "东方卫视"},
    ]
    entries = [
        M3UEntry(name="任意名称", url="http://a", channel_id="CCTV-1"),
        M3UEntry(name="东方卫视高清", url="http://b", channel_id="unknown"),
        M3UEntry(name="完全无关", url="http://c", channel_id="unknown"),
    ]
    results = match_entries(entries, channels)
    assert results[0].channel_id == "CCTV-1"
    assert results[0].matched_by == "tvg_id"
    assert results[1].channel_id == "东方卫视"
    assert results[1].matched_by == "normalized_name"
    assert results[2].channel_id is None


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def find(self, query=None, projection=None):
        def matches(row):
            for key, value in (query or {}).items():
                if key == "$or":
                    if not any(
                        all(row.get(k) == v for k, v in clause.items()) for clause in value
                    ):
                        return False
                elif isinstance(value, dict):
                    if "$ne" in value and row.get(key) == value["$ne"]:
                        return False
                    if "$in" in value and row.get(key) not in value["$in"]:
                        return False
                    if "$nin" in value and row.get(key) in value["$nin"]:
                        return False
                elif row.get(key) != value:
                    return False
            return True

        return [dict(row) for row in self.rows if matches(row)]

    def find_one(self, query=None, projection=None):
        rows = self.find(query, projection)
        return rows[0] if rows else None

    def count_documents(self, query=None):
        return len(self.find(query))

    def update_one(self, query, update):
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                for key, value in update.get("$set", {}).items():
                    row[key] = value
                break

    def update_many(self, query, update):
        for row in self.rows:
            if all(row.get(k) == v for k, v in query.items()):
                for key, value in update.get("$set", {}).items():
                    row[key] = value

    def insert_one(self, document):
        self.rows.append(dict(document))

    def delete_one(self, query):
        for index, row in enumerate(self.rows):
            if all(row.get(k) == v for k, v in query.items()):
                self.rows.pop(index)
                return

    def delete_many(self, query):
        self.rows = [row for row in self.rows if not all(row.get(k) == v for k, v in query.items())]


class FakeDatabase:
    def __init__(self, channels=None, sources=None, custom_sources=None):
        self.channels = FakeCollection(channels or [])
        self.sources = FakeCollection(sources or [])
        self.custom_sources = FakeCollection(custom_sources or [])


def test_create_source_appends_backup_without_touching_primary():
    primary = source("http://primary", source_id="s-primary", locked=True, origin="seed")
    db = FakeDatabase(
        channels=[channel(m3u_url="http://primary", stream_candidates=["http://primary"])],
        sources=[primary],
    )
    created = store.create_source(
        db,
        channel_id="C1",
        url="http://backup",
        label="备用",
    )
    assert created["priority"] == 1
    assert db.channels.rows[0]["preferred_source_id"] == "s-primary"
    assert db.channels.rows[0]["active_source_id"] == "s-primary"
    assert db.channels.rows[0]["m3u_url"] == "http://primary"


def test_set_preferred_reorders_and_relocks():
    primary = source("http://primary", source_id="s-primary", locked=True)
    backup = source("http://backup", source_id="s-backup", priority=1)
    db = FakeDatabase(
        channels=[channel(m3u_url="http://primary", stream_candidates=["http://primary", "http://backup"])],
        sources=[primary, backup],
    )
    store.set_preferred(db, "C1", "s-backup")
    assert db.channels.rows[0]["preferred_source_id"] == "s-backup"
    assert db.channels.rows[0]["m3u_url"] == "http://backup"
    locked = [row for row in db.sources.rows if row.get("locked")]
    assert [row["source_id"] for row in locked] == ["s-backup"]


def test_delete_seed_source_is_rejected():
    import pytest

    db = FakeDatabase(sources=[source("http://seed", source_id="s", origin="seed")])
    with pytest.raises(ValueError):
        store.delete_source(db, "s")


def test_candidates_start_with_active_then_primary_then_backups():
    rows = [
        source("http://primary", source_id="s-primary", locked=True, priority=0),
        source("http://backup", source_id="s-backup", priority=1),
        source("http://third", source_id="s-third", priority=2),
    ]
    db = FakeDatabase(
        sources=rows,
        channels=[
            channel(
                active_source_id="s-backup",
                active_state="fallback",
                primary_retry_at=datetime.now(UTC) - timedelta(seconds=120),
            )
        ],
    )
    ids = [row["source_id"] for row in resolver.candidates(db, db.channels.rows[0])]
    # A due retry probes the locked primary first.
    assert ids[0] == "s-primary"
    assert "s-backup" in ids and "s-third" in ids


def test_candidates_retry_not_due_keeps_fallback_first():
    rows = [
        source("http://primary", source_id="s-primary", locked=True, priority=0),
        source("http://backup", source_id="s-backup", priority=1),
    ]
    db = FakeDatabase(
        sources=rows,
        channels=[
            channel(
                active_source_id="s-backup",
                active_state="fallback",
                primary_retry_at=datetime.now(UTC),
            )
        ],
    )
    ids = [row["source_id"] for row in resolver.candidates(db, db.channels.rows[0])]
    assert ids[0] == "s-backup"


def test_m3u_preview_marks_existing_and_unbound(tmp_path):
    m3u = tmp_path / "backup.m3u"
    m3u.write_text(
        """#EXTM3U
#EXTINF:-1 tvg-id="CCTV-1",CCTV-1
http://example.com/new.m3u8
#EXTINF:-1 tvg-id="不存在的频道",不存在的频道
http://example.com/unknown.m3u8
""",
        encoding="utf-8",
    )
    existing = source(
        "http://example.com/new.m3u8",
        source_id="existing",
        origin="seed",
        channel_id="CCTV-1",
    )
    db = FakeDatabase(
        channels=[{"channel_id": "CCTV-1", "name": "CCTV-1"}],
        sources=[existing],
    )
    preview = preview_import(db, source=str(m3u))
    actions = {row["url"]: row["action"] for row in preview["rows"]}
    assert actions["http://example.com/new.m3u8"] == "exists"
    assert actions["http://example.com/unknown.m3u8"] == "unbound"


def test_replace_user_sources_keeps_synced_rows_and_sets_primary():
    seed = source("http://seed", source_id="s-seed", origin="seed", priority=0)
    custom = source("http://custom", source_id="s-custom", origin="custom", priority=1)
    backup = source("http://backup", source_id="s-backup", origin="custom", priority=2)
    db = FakeDatabase(
        channels=[channel(m3u_url="http://seed", stream_candidates=["http://seed"])],
        sources=[seed, custom, backup],
    )
    result = store.replace_user_sources(
        db,
        "C1",
        [{"url": "http://seed", "name": "内置"}, {"url": "http://backup", "name": "备用"}],
        primary_index=0,
    )
    assert result["preferred_source_id"] == "s-seed"
    # ``http://custom`` was user-created and is absent from the new list.
    assert result["removed"] == 1
    assert [row["source_id"] for row in db.sources.rows] == ["s-seed", "s-backup"]
    assert db.channels.rows[0]["preferred_source_id"] == "s-seed"


def test_replace_user_sources_rejects_unknown_channel():
    import pytest

    with pytest.raises(KeyError):
        store.replace_user_sources(FakeDatabase(), "missing", [{"url": "http://x", "name": ""}])
