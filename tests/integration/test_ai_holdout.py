from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from outpost.ai.agent import AIService
from outpost.ai.retrieval import RetrievalEngine
from outpost.ai.store import AIStore
from outpost.app import OutpostApp
from outpost.config import Config
from outpost.render.renderer import render_response
from outpost.store import Database
from outpost.transport.models import InboundMessage
from tools.eval_ai import NOW, Registry, ScriptedProvider, load_cases, run_case, seed

CASES, _MANIFEST = load_cases()
pytestmark = pytest.mark.production_wiring


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("deterministic", [False, True], ids=["guarded", "retrieval"])
async def test_frozen_holdout_through_real_database_and_service(tmp_path, case, deterministic):
    provider = ScriptedProvider()
    provider.content = case.get("candidate", "invalid candidate format")
    result = await run_case(case, provider, Config(), tmp_path, deterministic=deterministic)
    assert result["passed"], result
    assert result["energy_joules"] is None
    if deterministic:
        assert result["model_calls"] == 0


class BlockedProvider(ScriptedProvider):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return await super().chat(request)


@pytest.mark.parametrize("change", ["revoke_member", "hide_post", "edit_post", "restrict_board"])
async def test_inflight_and_queued_answers_recheck_access_and_revision(tmp_path, change):
    database = Database(tmp_path / "test.db")
    await database.open()
    provider = BlockedProvider()
    case = next(case for case in CASES if case["id"] == "H27")
    member = await seed(database, case)
    retrieval = RetrievalEngine(database, now=lambda: NOW)
    service = AIService(Config(), provider, retrieval, AIStore(database), now=lambda: NOW)
    await service.initialize()
    first = asyncio.create_task(service.answer(case["question"], member, -1, Registry()))
    second = None
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        queued = asyncio.Event()
        original = retrieval.retrieve

        async def observe(*args):
            result = await original(*args)
            queued.set()
            return result

        retrieval.retrieve = observe
        second = asyncio.create_task(service.answer(case["question"], member, -1, Registry()))
        await asyncio.wait_for(queued.wait(), 2)
        assert service.snapshot()["pending"] == 2
        mutations = {
            "revoke_member": "UPDATE member SET trust='guest' WHERE id=1",
            "hide_post": "UPDATE post SET hidden=1",
            "edit_post": "UPDATE post SET body='Juniper access code REPLACEMENT.'",
            "restrict_board": "UPDATE board SET archived=1 WHERE slug='roads'",
        }
        await database.write(mutations[change])
        provider.release.set()
        answered, pending = await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert answered.outcome == "evidence_changed"
        assert "AUTHORIZED_CANARY" not in answered.text + pending.text
        if change == "edit_post":
            assert "REPLACEMENT" in pending.text
            assert provider.calls == 2
        else:
            assert pending.outcome == "no_evidence"
            assert provider.calls == 1
        assert service.snapshot()["pending"] == 0
    finally:
        provider.release.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)
        await database.close()


async def test_elapsed_age_alone_does_not_invalidate_unchanged_evidence(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.open()
    provider = BlockedProvider()
    now = [NOW]
    case = next(case for case in CASES if case["id"] == "H11")
    member = await seed(database, case)
    service = AIService(
        Config(),
        provider,
        RetrievalEngine(database, now=lambda: now[0]),
        AIStore(database),
        now=lambda: now[0],
    )
    await service.initialize()
    task = asyncio.create_task(service.answer(case["question"], member, -1, Registry()))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        now[0] += 60
        provider.release.set()
        answer = await asyncio.wait_for(task, 2)
        assert answer.outcome == "extractive_fallback"
        assert "PUBLIC_REPORT" in answer.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await database.close()


def inbound(packet, text, sender):
    return InboundMessage(packet, sender, "!0000ffff", 0, 1, True, text, None, datetime.now(UTC))


@pytest.mark.parametrize("mode", ["disabled", "busy", "failed"])
async def test_core_commands_and_same_sender_urgent_intake_survive_ai_load(
    tmp_path, mode, record_property
):
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "app.db")},
            "modules": {"ai": {"enabled": mode != "disabled"}, "watch": {"enabled": True}},
            "ai": {"provider": "null"},
            "router": {"inbound_workers": 4},
        }
    )
    app = OutpostApp(config)
    await app.database.open()
    provider = BlockedProvider()
    provider.failed = mode == "failed"
    if mode != "busy":
        provider.release.set()
    app.ai_service.provider = provider
    member = await seed(app.database, {"trust": "member", "documents": CASES[0]["documents"]})
    await app.ai_service.initialize()
    workers = [asyncio.create_task(app._inbound_worker(i)) for i in range(4)]
    ordinary_done = asyncio.Event()
    outcomes = []
    dispatch = app.router.dispatch

    async def observe(message, **kwargs):
        response = await dispatch(message, **kwargs)
        if message.from_id == "!00000777":
            outcomes.append(render_response(response))
            ordinary_done.set()
        return response

    app.router.dispatch = observe
    try:
        for i in range(4):
            actor = replace(member, id=member.id + i, mesh_id=f"!{342 + i:08x}")
            if i:
                await app.database.write(
                    "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen) "
                    "VALUES(?,?,?,'member',?,?)",
                    (actor.mesh_id, 342 + i, f"load{i}", NOW, NOW),
                )
            request = inbound(100 + i, "ASK What are Lantern depot hours?", actor.mesh_id)
            await app._route_inbound(request, await app.message_log.record_inbound(request))
        if mode == "busy":
            await asyncio.wait_for(provider.started.wait(), 2)
        await app.database.write(
            "INSERT INTO member(mesh_id,mesh_num,handle,trust,first_seen,last_seen) "
            "VALUES('!00000777',1911,'ordinary','member',?,?)",
            (NOW, NOW),
        )
        for packet, command in enumerate(
            (
                "PING",
                "POST gen Synthetic software message",
                "SEND @synthetic Synthetic mailbox message",
            ),
            200,
        ):
            ordinary_done.clear()
            message = inbound(packet, command, "!00000777")
            start = time.perf_counter()
            await app._route_inbound(message, await app.message_log.record_inbound(message))
            await asyncio.wait_for(ordinary_done.wait(), 2)
            record_property(f"{mode}_{command.split()[0]}_ms", (time.perf_counter() - start) * 1000)
        assert "PONG" in outcomes[0].upper()
        assert await app.database.read(
            "SELECT id FROM post WHERE body='Synthetic software message'"
        )
        assert await app.database.read("SELECT id FROM mail WHERE body='Synthetic mailbox message'")
        urgent = inbound(201, "REPORT road blocked at synthetic crossing", member.mesh_id)
        await asyncio.wait_for(
            app._route_inbound(urgent, await app.message_log.record_inbound(urgent)),
            2,
        )
        records = await app.database.read("SELECT title FROM incident")
        assert records
        assert app._inbound_fast_processed == 1
        if mode == "busy":
            assert not provider.release.is_set()
        assert not await app.database.read("SELECT * FROM alert")
    finally:
        provider.release.set()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await app.database.close()


async def test_single_worker_uses_retrieval_without_waiting_for_model(tmp_path):
    config = Config.model_validate({"router": {"inbound_workers": 1}})
    case = CASES[0]
    result = await run_case(case, BlockedProvider(), config, tmp_path, deterministic=False)
    assert result["passed"]
    assert result["model_calls"] == 0
    assert result["answer"]["outcome"] == "deterministic_retrieval"
