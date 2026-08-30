from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from outpost.app import OutpostApp
from outpost.clock import Clock
from outpost.config import AirtimeConfig, Config
from outpost.store import Database
from outpost.store.outbox import OutboxStore
from outpost.transport.governor import AirtimeGovernor
from outpost.transport.models import RadioLink
from outpost.transport.simulated import SimulatedRadioLink


@asynccontextmanager
async def production_app(config: Config) -> AsyncIterator[OutpostApp]:
    """Open the same object graph and durable database path used by the appliance."""
    app = OutpostApp(config)
    await app.database.open()
    try:
        yield app
    finally:
        await app.database.close()


@asynccontextmanager
async def fresh_install(
    config: Config,
    *,
    member_mesh_ids: Sequence[str] = ("!00000001", "!00000002"),
) -> AsyncIterator[OutpostApp]:
    """Open a production-wired app with discovered members but no promoted audience."""
    async with production_app(config) as app:
        for mesh_id in member_mesh_ids:
            await app.router.members.resolve(mesh_id)
        assert (
            await app.database.read(
                "SELECT 1 FROM member WHERE trust IN ('trusted','responder','operator')"
            )
            == []
        )
        assert await app.database.read("SELECT 1 FROM watch_event WHERE closed_at IS NULL") == []
        yield app


def production_governor(
    database: Database,
    clock: Clock,
    *,
    link: RadioLink | None = None,
    airtime: AirtimeConfig | None = None,
    preset: str = "LONG_FAST",
    region: str | None = None,
    timezone: str = "UTC",
) -> AirtimeGovernor:
    """Construct the governor exactly as OutpostApp does, including its durable outbox."""
    return AirtimeGovernor(
        link or SimulatedRadioLink(),
        airtime or AirtimeConfig(),
        clock,
        preset=preset,
        region=region,
        outbox=OutboxStore(database),
        timezone=timezone,
    )
