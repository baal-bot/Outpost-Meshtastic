#!/usr/bin/env python3
"""Measure warm-dashboard idle cost on the current Outpost host."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from playwright.async_api import async_playwright
from prometheus_client import REGISTRY

from outpost.app import OutpostApp
from outpost.config import Config
from outpost.store import Database

PROVIDER_HOSTS = ("api.weather.gov", "api.open-meteo.com", "earthquake.usgs.gov")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds",
        type=int,
        default=300,
        help="seconds to observe each of the visible and hidden phases (default: 300)",
    )
    return parser.parse_args()


def rss_mib() -> float:
    resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
    return resident_pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576


def provider_counts() -> dict[str, float]:
    return {
        host: float(
            REGISTRY.get_sample_value("outpost_environment_provider_requests_total", {"host": host})
            or 0
        )
        for host in PROVIDER_HOSTS
    }


def difference(after: dict[str, float], before: dict[str, float]) -> dict[str, int]:
    return {key: round(after[key] - before[key]) for key in after}


def load_probe_config(database_path: Path) -> Config:
    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "config/config.example.yaml").read_text())
    data["store"]["path"] = str(database_path)
    data["web"].update({"bind": "127.0.0.1", "port": 8081})
    data["node"]["location"] = {"lat": 40.4406, "lon": -79.9959}
    return Config.model_validate(data)


async def phase(
    page: Any, seconds: int, requests: Counter[str], database: Database
) -> dict[str, Any]:
    requests.clear()
    start_cpu = time.process_time()
    start_rss = rss_mib()
    start_pool = database.read_pool_status()
    start_providers = provider_counts()
    started = time.monotonic()
    await asyncio.sleep(seconds)
    wall_seconds = time.monotonic() - started
    end_pool = database.read_pool_status()
    return {
        "seconds": round(wall_seconds, 2),
        "api_requests": sum(requests.values()),
        "api_requests_by_path": dict(sorted(requests.items())),
        "process_cpu_percent": round((time.process_time() - start_cpu) / wall_seconds * 100, 2),
        "rss_mib": round(rss_mib(), 2),
        "rss_growth_mib": round(rss_mib() - start_rss, 2),
        "db_connections_opened": end_pool["opened"] - start_pool["opened"],
        "db_queries": end_pool["queries"] - start_pool["queries"],
        "provider_requests": difference(provider_counts(), start_providers),
    }


async def measure(seconds: int) -> dict[str, Any]:
    if seconds < 10:
        raise ValueError("--seconds must be at least 10")
    browser_path = next(
        (
            path
            for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
            if (path := shutil.which(name)) is not None
        ),
        None,
    )
    if browser_path is None:
        raise RuntimeError("Chromium is required for the dashboard idle probe")

    with tempfile.TemporaryDirectory(prefix="outpost-dashboard-probe-") as temporary:
        application = OutpostApp(load_probe_config(Path(temporary) / "outpost.db"))
        await application.database.open()
        setup = await application.web_auth.ensure_credential()
        assert setup is not None
        first = await application.web_auth.login(setup.path.read_text().strip(), "idle-probe")
        assert first is not None
        await application.web_auth.change_password(first[0], "", "performance-probe-42")
        login = await application.web_auth.login("performance-probe-42", "idle-probe")
        assert login is not None

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(application.web, host="127.0.0.1", port=port, log_level="critical")
        )
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:  # noqa: ASYNC110 - uvicorn exposes a polling flag only.
                await asyncio.sleep(0.01)

            requests: Counter[str] = Counter()
            page_errors: list[str] = []
            providers_before_load = provider_counts()
            async with async_playwright() as runtime:
                browser = await runtime.chromium.launch(headless=True, executable_path=browser_path)
                context = await browser.new_context()
                await context.add_cookies(
                    [
                        {
                            "name": "outpost_session",
                            "value": login[0],
                            "url": f"http://127.0.0.1:{port}",
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
                page = await context.new_page()
                page.on(
                    "request",
                    lambda request: (
                        requests.update(
                            [request.url.split(f"http://127.0.0.1:{port}", 1)[-1].split("?", 1)[0]]
                        )
                        if "/api/" in request.url
                        else None
                    ),
                )
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                await page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
                await asyncio.sleep(5)
                warmup = {
                    "provider_requests": difference(provider_counts(), providers_before_load),
                    "db_pool": application.database.read_pool_status(),
                }
                visible = await phase(page, seconds, requests, application.database)
                await page.evaluate(
                    """() => {
                      Object.defineProperty(document, 'hidden', {configurable: true, value: true});
                      document.dispatchEvent(new Event('visibilitychange'));
                    }"""
                )
                await asyncio.sleep(1)
                hidden = await phase(page, seconds, requests, application.database)
                await browser.close()

            return {
                "hardware": {
                    "machine": os.uname().machine,
                    "kernel": os.uname().release,
                    "cpu_count": os.cpu_count(),
                },
                "page_errors": page_errors,
                "warmup": warmup,
                "visible": visible,
                "hidden": hidden,
            }
        finally:
            server.should_exit = True
            await server_task
            listener.close()
            await application.database.close()


def main() -> None:
    args = parse_args()
    print(json.dumps(asyncio.run(measure(args.seconds)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
