from __future__ import annotations

import asyncio

import uvicorn

from outpost.app import OutpostApp
from outpost.config import load_config
from outpost.systemd import notify, watchdog


def main() -> None:
    config = load_config()
    application = OutpostApp(config)

    async def serve() -> None:
        await application.startup()
        watchdog_task = asyncio.create_task(watchdog(application.clock), name="systemd-watchdog")
        server = uvicorn.Server(
            uvicorn.Config(
                application.web, host=config.web.bind, port=config.web.port, log_level="info"
            )
        )
        notify("READY=1")
        try:
            await server.serve()
        finally:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)
            await application.shutdown()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
