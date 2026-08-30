from __future__ import annotations

import asyncio

import uvicorn

from outpost.app import OutpostApp
from outpost.config import load_config
from outpost.systemd import notify, watchdog
from outpost.web.tiles import inspect_tile_pack
from outpost.web.transport import uvicorn_options


def main() -> None:
    config = load_config()
    if config.environment_overrides:
        print(
            "Outpost configuration overridden by environment: "
            + ", ".join(config.environment_overrides),
            flush=True,
        )
    channel_policy = [
        {
            "channel": index,
            "name": policy.name,
            "bbs": policy.bbs,
            "ai": policy.ai,
            "alerts": policy.alerts,
            "accept_reports": policy.accept_reports,
        }
        for index, policy in sorted(config.channels.items())
    ]
    print(f"Outpost effective channel policy: {channel_policy!r}", flush=True)
    tile_pack = inspect_tile_pack(config.store.tiles_path)
    print(
        "Outpost offline tile pack: "
        f"path={tile_pack.root} status={tile_pack.state} detail={tile_pack.detail}",
        flush=True,
    )
    server_options = uvicorn_options(config.web)
    application = OutpostApp(config)

    async def serve() -> None:
        await application.startup()
        watchdog_task = asyncio.create_task(
            watchdog(application.clock, application.core_tasks_healthy),
            name="systemd-watchdog",
        )
        server = uvicorn.Server(
            uvicorn.Config(
                application.web,
                host=config.web.bind,
                port=config.web.port,
                log_level="info",
                **server_options,
            )
        )
        notify("READY=1")
        server_task = asyncio.create_task(server.serve(), name="web-server")
        failure_task = asyncio.create_task(
            application.wait_for_task_failure(), name="background-task-failure"
        )
        restart_task = asyncio.create_task(application.wait_for_restart(), name="recovery-restart")
        try:
            done, _ = await asyncio.wait(
                {server_task, failure_task, restart_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if failure_task in done:
                reason = failure_task.result()
                server.should_exit = True
                await server_task
                raise RuntimeError(f"critical Outpost task failed: {reason}")
            if restart_task in done:
                server.should_exit = True
            for task in (failure_task, restart_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(failure_task, restart_task, return_exceptions=True)
            await server_task
        finally:
            if not server_task.done():
                server.should_exit = True
                await server_task
            if not failure_task.done():
                failure_task.cancel()
                await asyncio.gather(failure_task, return_exceptions=True)
            if not restart_task.done():
                restart_task.cancel()
                await asyncio.gather(restart_task, return_exceptions=True)
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)
            await application.shutdown()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
