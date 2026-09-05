"""Indexed producer paging over large synthetic histories; never transmits RF."""

import json
import time
import tracemalloc
from dataclasses import replace

import pytest

from outpost.clock import VirtualClock
from outpost.fed import FederationPeerService, FederationSyncService, FrameCodec, MessageType
from outpost.fed.revisions import MAX_BOARDS, SCAN_LIMIT
from outpost.store import Database

pytestmark = pytest.mark.production_wiring


@pytest.fixture
async def history(tmp_path):
    database = Database(tmp_path / "synthetic.db")
    await database.open()
    peers = FederationPeerService(database, VirtualClock(), "!local")
    await peers.discover("!remote", "Synthetic peer", 1, {"reconciliation": 2}, "radio")
    await database.write("UPDATE fed_peer SET state='active' WHERE mesh_id='!remote'")
    peer = await peers.update_sync_policy(
        "!remote",
        boards=["gen"],
        sync_incidents=True,
        relay_alerts=True,
        quota_items_per_hour=500,
        incident_lat=0,
        incident_lon=0,
        incident_radius_km=10,
    )
    await database.write("UPDATE board SET federated=1 WHERE slug='gen'")
    sync = FederationSyncService(database, "!local")
    yield database, sync, peer
    await database.close()


async def seed(database, size):
    """Seed outside measurements, in bounded SQL statements rather than Python rows."""
    async with database.transaction() as tx:
        await tx.write(
            "INSERT INTO board(slug,title,federated,created_at) "
            "VALUES('private','Synthetic private',0,1)"
        )
        for slug in ("gen", "private"):
            thread = await tx.write(
                "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
                "SELECT ?,id,'Synthetic subject','!local',1,1 FROM board WHERE slug=?",
                (f"thread:{slug}", slug),
            )
            await tx.write(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<?) "
                "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
                "SELECT ?||x,?,x,'Synthetic','!local','Synthetic post body',1 FROM n",
                (size, f"{slug}:", thread),
            )
        await tx.write(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<?) "
            "INSERT INTO incident(uid,local_ref,type,severity,title,reporter_label,origin_node,"
            "created_at,updated_at,lat,lon) SELECT 'incident:'||x,x,'road','caution',"
            "'Synthetic road obstruction','Synthetic','!local',1,1,"
            "CASE WHEN x%3=0 THEN NULL ELSE 0 END,CASE WHEN x%3=1 THEN 90 ELSE 0 END FROM n",
            (size,),
        )
        await tx.write(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<?) "
            "INSERT INTO alert(uid,severity,headline,source,channels,raised_by,raised_at) "
            "SELECT 'alert:'||x,'urgent','Synthetic alert','operator','[0]','Synthetic',1 FROM n",
            (size,),
        )


@pytest.mark.parametrize("size", [300, 30_000])
async def test_large_history_page_cost_is_bounded_and_wire_cost_is_separate(
    history, monkeypatch, record_property, size
):
    database, sync, peer = history
    await seed(database, size)
    reports = []
    for after in (0, size, size * 2, size * 3):
        queries = []
        ordinary_read, writer_read = database.read, database._writer_read

        def capture(reader, captured=queries):
            async def measured(sql, params=()):
                rows = await reader(sql, params)
                captured.append((sql, params, len(rows)))
                return rows

            return measured

        started = time.perf_counter()
        tracemalloc.start()
        try:
            with monkeypatch.context() as patch:
                patch.setattr(database, "read", capture(ordinary_read))
                patch.setattr(database, "_writer_read", capture(writer_read))
                page = await sync.revisions.page(peer, {"cycle": "a" * 32, "after": after})
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        seek, bindings, returned = next(query for query in queries if "UNION ALL" in query[0])
        plan = await database.read("EXPLAIN QUERY PLAN " + seek, bindings)
        details = [row["detail"] for row in plan]
        assert (
            sum(
                "SEARCH fed_revision USING COVERING INDEX idx_fed_revision_stream" in line
                for line in details
            )
            == 3
        )
        assert max(query[2] for query in queries) <= SCAN_LIMIT + 1
        assert len(queries) <= 30
        assert len(page["items"]) == 8
        assert page["after"] == after and page["next"] > after
        assert peak < 2 * 1024 * 1024
        # A generous target-host ceiling detects accidental whole-history work;
        # recorded latency provides the actual baseline, not an RF SLA.
        assert elapsed < 2
        frames = FrameCodec().encode(MessageType.SYNC_MANIFEST, page, 1, bytes(range(32)))
        assert all(len(frame) <= 188 for frame in frames)
        reports.append(
            {
                "retained_heads": size * 4,
                "after": after,
                "returned_heads": returned,
                "sql_calls": len(queries),
                "python_peak_bytes": peak,
                "seconds": round(elapsed, 6),
                "wire_frames": len(frames),
                "wire_bytes": sum(map(len, frames)),
                "query_plan": details,
            }
        )
    record_property("producer_page_cost", json.dumps(reports, sort_keys=True))


async def test_mixed_stream_pages_preserve_scope_order_and_equal_timestamp_records(history):
    database, sync, peer = history
    await seed(database, 24)
    seen = []
    request = {"cycle": "b" * 32, "after": 0}
    while True:
        page = await sync.revisions.page(peer, request)
        assert page["next"] > request["after"]
        for item in page["items"]:
            assert item["s"] != "board:private"
            exported = await sync.export_items(peer, [{"stream": item["s"], "uid": item["u"]}])
            assert len(exported) == 1
            if item["s"] == "incidents":
                assert sync.incident_allowed(
                    peer, exported[0]["payload"]["lat"], exported[0]["payload"]["lon"]
                )
            seen.append((item["r"], item["s"], item["u"]))
        if page["done"]:
            break
        # A continuation checkpoint can be serialized and used by a fresh service.
        request = json.loads(json.dumps({**page, "after": page["next"]}))
        sync = FederationSyncService(database, "!local")
    assert seen == sorted(seen)
    assert len(seen) == len({(stream, uid) for _, stream, uid in seen}) == 64
    assert {stream for _, stream, _ in seen} == {"board:gen", "incidents", "alerts"}
    for stream, uid in (("board:private", "!local:private:1"), ("incidents", "!local:incident:1")):
        assert await sync.export_items(peer, [{"stream": stream, "uid": uid}]) == []
    await database.write("UPDATE thread SET hidden=1 WHERE uid='thread:gen'")
    hidden = await sync.revisions.page(peer, {"cycle": "c" * 32, "after": 0})
    assert all(item["s"] != "board:gen" for item in hidden["items"])
    assert await sync.export_items(peer, [{"stream": "board:gen", "uid": "!local:gen:1"}]) == []


async def test_archived_boards_are_withheld_from_both_manifest_modes_and_export(history):
    database, sync, peer = history
    await seed(database, 24)
    await database.write("UPDATE board SET archived=1 WHERE slug='gen'")
    page = await sync.revisions.page(peer, {"cycle": "f" * 32})
    assert all(item["s"] != "board:gen" for item in page["items"])
    assert all(item.stream != "board:gen" for item in await sync.manifest(peer))
    assert await sync.export_items(peer, [{"stream": "board:gen", "uid": "!local:gen:1"}]) == []
    await database.write("UPDATE board SET archived=0 WHERE slug='gen'")
    await database.write("UPDATE thread SET hidden=1 WHERE uid='thread:gen'")
    assert all(item.stream != "board:gen" for item in await sync.manifest(peer))


async def test_empty_geographic_pages_are_scan_bounded_and_resume(history):
    database, sync, peer = history
    await seed(database, 120)
    await database.write("UPDATE incident SET lat=0,lon=90")
    peer = replace(peer, boards=[], relay_alerts=False)
    page = await sync.revisions.page(peer, {"cycle": "d" * 32})
    assert page["items"] == [] and page["done"] is False
    through = (
        await database.read(
            "SELECT COUNT(*) FROM fed_revision WHERE stream='incidents' AND revision<=?",
            (page["next"],),
        )
    )[0][0]
    assert through == SCAN_LIMIT
    last = await sync.revisions.page(peer, {**page, "after": page["next"]})
    assert last["items"] == [] and last["done"] is True
    assert last["next"] == last["snapshot"]


async def test_disabled_streams_do_not_scan_their_retained_heads(history):
    database, sync, peer = history
    await seed(database, 300)
    sync.module_enabled = lambda _: False
    page = await sync.revisions.page(peer, {"cycle": "e" * 32})
    assert page["items"] == [] and page["done"] is True
    assert page["next"] == page["snapshot"]
    with pytest.raises(ValueError, match="policy is too large"):
        await sync.revisions.page(
            replace(peer, boards=["gen"] * (MAX_BOARDS + 1)), {"cycle": "e" * 32}
        )


async def test_maximum_stream_merge_is_bounded_before_final_limit(history, monkeypatch):
    database, sync, peer = history
    await seed(database, 120)
    slugs = ["gen"]
    for number in range(MAX_BOARDS - 1):
        slug = f"public{number}"
        slugs.append(slug)
        board = await database.write(
            "INSERT INTO board(slug,title,federated,created_at) VALUES(?,'Synthetic',1,1)", (slug,)
        )
        thread = await database.write(
            "INSERT INTO thread(uid,board_id,subject,origin_node,created_at,last_post_at) "
            "VALUES(?,?,'Synthetic','!local',1,1)",
            (slug, board),
        )
        await database.write(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<120) "
            "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,created_at) "
            "SELECT ?||':'||x,?,x,'Synthetic','!local','Synthetic',1 FROM n",
            (slug, thread),
        )
    statements = []
    read = database._writer_read

    async def capture(sql, params=()):
        rows = await read(sql, params)
        if "UNION ALL" in sql:
            statements.append((sql, params, rows))
        return rows

    monkeypatch.setattr(database, "_writer_read", capture)
    page = await sync.revisions.page(replace(peer, boards=slugs), {"cycle": "a" * 32})
    assert len(page["items"]) == 8 and page["done"] is False
    sql, params, rows = statements[0]
    assert sql.count("INDEXED BY idx_fed_revision_stream") == MAX_BOARDS + 2
    assert sql.count("LIMIT ?") == MAX_BOARDS + 3
    assert len(rows) == SCAN_LIMIT + 1
    assert all(params[offset] == SCAN_LIMIT + 1 for offset in range(3, len(params), 4))


async def test_edits_to_each_stream_move_beyond_snapshot_then_resume(history):
    database, sync, peer = history
    await seed(database, 24)
    first = await sync.revisions.page(peer, {"cycle": "a" * 32})
    changed = {
        ("board:gen", "!local:gen:24"),
        ("incidents", "!local:incident:2"),
        ("alerts", "!local:alert:24"),
    }
    async with database.transaction() as tx:
        await tx.write("UPDATE post SET body='Edited synthetic post' WHERE uid='gen:24'")
        await tx.write(
            "UPDATE incident SET title='Edited synthetic incident' WHERE uid='incident:2'"
        )
        await tx.write("UPDATE alert SET headline='Edited synthetic alert' WHERE uid='alert:24'")
    page = first
    old = {(item["s"], item["u"]) for item in page["items"]}
    while not page["done"]:
        sync = FederationSyncService(database, "!local")
        page = await sync.revisions.page(
            peer, json.loads(json.dumps({**page, "after": page["next"]}))
        )
        identities = {(item["s"], item["u"]) for item in page["items"]}
        assert not old.intersection(identities)
        old.update(identities)
    assert len(old) == 61 and not old.intersection(changed)
    fresh = await sync.revisions.page(peer, {"cycle": "b" * 32, "after": first["snapshot"]})
    assert fresh["done"] is True
    assert {(item["s"], item["u"]) for item in fresh["items"]} == changed
    assert len(old | changed) == 64
