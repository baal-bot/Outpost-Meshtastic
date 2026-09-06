from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager

import httpx
import pytest

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.fed.review import FederationReviewService, ReviewConflict
from outpost.store import Transaction
from outpost.transport.simulated import SimulatedRadioLink

pytestmark = [pytest.mark.asyncio, pytest.mark.production_wiring]


@asynccontextmanager
async def review_app(tmp_path):
    clock = VirtualClock()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "review.db")},
            "modules": {name: {"enabled": True} for name in ("fed", "watch", "bbs")},
        }
    )
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock))
    await app.database.open()
    try:
        await app.federation.discover("!remote", "Remote", 1, {}, "radio")
        await app.database.write(
            "UPDATE fed_peer SET state='active',relay_alerts=1,sync_incidents=1,boards='[\"gen\"]'"
        )
        yield app
    finally:
        await app.database.close()


async def quarantine(app, *, title="Original alert", revision=1, uid="!remote:alert:1"):
    peer = await app.federation.by_mesh_id("!remote")
    await app.federation_sync.quarantine(
        peer,
        {
            "stream": "alerts",
            "uid": uid,
            "epoch": "a" * 32,
            "revision": revision,
            "payload": {"headline": title, "severity": "urgent", "raised_at": 100},
        },
        100,
    )
    row = (await app.database.read("SELECT id FROM fed_inbox_item WHERE uid=?", (uid,)))[0]
    preview = await app.operations_center.federation_item(row["id"])
    assert preview is not None
    return preview


async def decide(app, preview, action, actor="web:operator"):
    if action == "imported":
        return await app.import_federation_inbox_as(preview["id"], actor, preview["review_token"])
    return await FederationReviewService(app.database).reject(
        preview["id"], preview["review_token"], actor, "Reviewed rejection", 101
    )


@pytest.mark.parametrize("action", ["imported", "rejected"])
@pytest.mark.parametrize("replacement", ["payload", "revision_only", "peer_name"])
async def test_review_refuses_replacement_in_same_second(tmp_path, action, replacement):
    async with review_app(tmp_path) as app:
        preview = await quarantine(app)
        if replacement == "peer_name":
            await app.database.write("UPDATE fed_peer SET node_name='Renamed peer'")
        else:
            await quarantine(
                app,
                title="Changed alert" if replacement == "payload" else "Original alert",
                revision=2,
            )
        with pytest.raises(ReviewConflict):
            await decide(app, preview, action)
        assert not await app.database.read("SELECT id FROM alert")
        assert not await app.database.read(
            "SELECT id FROM audit_log WHERE action LIKE 'federation.inbox.%'"
        )
        assert (await app.database.read("SELECT state FROM fed_inbox_item"))[0][
            "state"
        ] == "pending"
        current = await app.operations_center.federation_item(preview["id"])
        assert current["review_token"] != preview["review_token"]
        await decide(app, current, action)


@pytest.mark.parametrize(
    "first,second", [("imported", "imported"), ("imported", "rejected"), ("rejected", "rejected")]
)
async def test_two_reviewers_commit_only_one_decision_and_audit(tmp_path, first, second):
    async with review_app(tmp_path) as app:
        preview = await quarantine(app)
        results = await asyncio.gather(
            decide(app, preview, first, "web:alice"),
            decide(app, preview, second, "mesh:!bob"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ReviewConflict) for result in results) == 1
        assert not [
            result
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, ReviewConflict)
        ]
        rows = await app.database.read(
            "SELECT actor_kind,actor_ref FROM audit_log WHERE action LIKE 'federation.inbox.%'"
        )
        assert len(rows) == 1
        item = (await app.database.read("SELECT state,reviewed_by FROM fed_inbox_item"))[0]
        assert item["reviewed_by"] == f"{rows[0]['actor_kind']}:{rows[0]['actor_ref']}"
        assert len(await app.database.read("SELECT id FROM alert")) == (item["state"] == "imported")


@pytest.mark.parametrize("action", ["imported", "rejected"])
async def test_review_check_waits_for_writer_and_reads_committed_replacement(tmp_path, action):
    async with review_app(tmp_path) as app:
        preview = await quarantine(app)
        async with app.database.transaction() as transaction:
            await transaction.write(
                "UPDATE fed_inbox_item SET payload_json=?",
                (
                    json.dumps(
                        {
                            "headline": "Changed under writer lock",
                            "severity": "urgent",
                            "raised_at": 100,
                        }
                    ),
                ),
            )
            task = asyncio.create_task(decide(app, preview, action))
            await asyncio.sleep(0)
            assert not task.done()
        with pytest.raises(ReviewConflict):
            await task
        assert not await app.database.read("SELECT id FROM alert")


@pytest.mark.parametrize("action", ["imported", "rejected"])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_review_audit_failure_rolls_back_domain_and_decision(
    tmp_path, monkeypatch, action, failure
):
    async with review_app(tmp_path) as app:
        preview = await quarantine(app)
        original = Transaction.write

        async def fail_audit(transaction, sql, params=()):
            if sql.startswith("INSERT INTO audit_log") and "federation.inbox." in str(params):
                raise failure("review audit interrupted")
            return await original(transaction, sql, params)

        with monkeypatch.context() as patch:
            patch.setattr(Transaction, "write", fail_audit)
            with pytest.raises(failure):
                await decide(app, preview, action)
        assert not await app.database.read("SELECT id FROM alert")
        assert not await app.database.read(
            "SELECT id FROM audit_log WHERE action LIKE 'federation.%import%'"
        )
        assert (await app.database.read("SELECT state FROM fed_inbox_item"))[0][
            "state"
        ] == "pending"
        await decide(app, preview, action)


@pytest.mark.parametrize("change", ["scope", "peer", "module"])
async def test_approval_rechecks_current_permission(tmp_path, change):
    async with review_app(tmp_path) as app:
        preview = await quarantine(app)
        if change == "scope":
            await app.database.write("UPDATE fed_peer SET relay_alerts=0")
        elif change == "peer":
            await app.database.write("UPDATE fed_peer SET state='pending'")
        else:
            app.config.modules.watch.enabled = False
        with pytest.raises(ValueError, match="no longer|disabled"):
            await decide(app, preview, "imported")
        assert not await app.database.read("SELECT id FROM alert")
        # Operators may still reject quarantined content from a revoked peer.
        await decide(app, preview, "rejected")


async def test_review_restart_duplicate_and_no_public_broadcast(tmp_path):
    async with review_app(tmp_path) as app:
        preview = await quarantine(app)
        duplicate = await quarantine(app)
        assert duplicate["review_token"] == preview["review_token"]
    # A closed Database owns a shut-down executor; an ordinary process restart
    # constructs a fresh service graph against the retained store.
    async with review_app(tmp_path) as app:
        assert (await app.operations_center.federation_item(preview["id"]))[
            "review_token"
        ] == preview["review_token"]
        await decide(app, preview, "imported")
        with pytest.raises(ReviewConflict):
            await decide(app, preview, "rejected")
        alert = (await app.database.read("SELECT channels,raised_by FROM alert"))[0]
        assert alert["channels"] == "[]" and alert["raised_by"].startswith("federation:")
        assert not await app.database.read("SELECT id FROM outbound_work")
        assert app.radio.sent == []


@pytest.mark.parametrize("change", ["board_scope", "board_archived", "valid"])
async def test_human_board_import_retains_transactional_destination_checks(tmp_path, change):
    async with review_app(tmp_path) as app:
        await app.database.write("UPDATE board SET federated=1 WHERE slug='gen'")
        peer = await app.federation.by_mesh_id("!remote")
        await app.federation_sync.quarantine(
            peer,
            {
                "stream": "board:gen",
                "uid": "!remote:post:1",
                "payload": {
                    "thread_uid": "!remote:thread:1",
                    "subject": "Reviewed board thread",
                    "author_label": "Remote member",
                    "body": "Reviewed content",
                    "created_at": 100,
                },
            },
            100,
        )
        item_id = (await app.database.read("SELECT id FROM fed_inbox_item"))[0]["id"]
        preview = await app.operations_center.federation_item(item_id)
        if change == "board_scope":
            await app.database.write("UPDATE fed_peer SET boards='[]'")
        elif change == "board_archived":
            await app.database.write("UPDATE board SET archived=1 WHERE slug='gen'")
        if change == "valid":
            assert await decide(app, preview, "imported") == "board:gen"
            assert len(await app.database.read("SELECT id FROM post")) == 1
            audit = (
                await app.database.read(
                    "SELECT detail FROM audit_log WHERE action='federation.inbox.import'"
                )
            )[0]
            assert json.loads(audit["detail"])["review_fingerprint"] == preview["review_token"]
        else:
            with pytest.raises(ValueError, match="no longer|not federated"):
                await decide(app, preview, "imported")
            assert not await app.database.read("SELECT id FROM post")
            assert not await app.database.read("SELECT id FROM thread")
            assert (await app.database.read("SELECT state FROM fed_inbox_item"))[0][
                "state"
            ] == "pending"


@pytest.mark.parametrize("action", ["imported", "rejected"])
async def test_review_tag_cannot_be_used_for_a_different_item(tmp_path, action):
    async with review_app(tmp_path) as app:
        first = await quarantine(app)
        second = await quarantine(app, uid="!remote:alert:2")
        second["review_token"] = first["review_token"]
        with pytest.raises(ReviewConflict):
            await decide(app, second, action)
        assert not await app.database.read("SELECT id FROM alert")
        assert {
            row["state"] for row in await app.database.read("SELECT state FROM fed_inbox_item")
        } == {"pending"}


@pytest.mark.parametrize("action", ["imported", "rejected"])
async def test_authenticated_web_requires_review_tag_and_returns_conflict(tmp_path, action):
    async with review_app(tmp_path) as app:
        preview = await quarantine(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.web), base_url="http://test"
        ) as client:
            path = f"/api/v1/federation/inbox/{preview['id']}"
            assert (await client.patch(path, json={"state": action})).status_code == 401
            password = secrets.token_urlsafe(24)
            account = await app.web_auth.create_account(
                "reviewer", "Reviewer", "operator", password, "test"
            )
            await app.database.write(
                "UPDATE web_account SET must_change=0 WHERE id=?", (account["id"],)
            )
            login = await client.post(
                "/api/v1/auth/login", json={"username": "reviewer", "password": password}
            )
            assert login.status_code == 200
            csrf = login.json()["csrf_token"]
            assert (
                await client.patch(
                    path, json={"state": action, "review_token": preview["review_token"]}
                )
            ).status_code == 403
            client.headers["x-csrf-token"] = csrf
            listed = (await client.get("/api/v1/federation/inbox")).json()["items"][0]
            assert (
                listed["review_token"] == preview["review_token"] and "payload_json" not in listed
            )
            for token in (None, "bad", "x" * 64):
                body = {"state": action}
                if token is not None:
                    body["review_token"] = token
                assert (await client.patch(path, json=body)).status_code == 422
            await quarantine(app, title="Unseen replacement", revision=2)
            stale = await client.patch(
                path, json={"state": action, "review_token": listed["review_token"]}
            )
            assert stale.status_code == 409 and stale.json()["error"]["code"] == "review_conflict"
            refreshed = (await client.get("/api/v1/federation/inbox")).json()["items"][0]
            response = await client.patch(
                path, json={"state": action, "review_token": refreshed["review_token"]}
            )
            assert response.status_code == 200
            assert (
                await client.patch(
                    path, json={"state": action, "review_token": refreshed["review_token"]}
                )
            ).status_code == 409
            audit = (
                await app.database.read(
                    "SELECT actor_kind,actor_ref FROM audit_log "
                    "WHERE action LIKE 'federation.inbox.%'"
                )
            )[0]
            assert tuple(audit) == ("web", "reviewer")
