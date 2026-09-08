"""Authenticated setup operations; downloads run outside the request event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from outpost.maps.regions import MAX_DOWNLOAD_BYTES, MapError, Region
from outpost.maps.setup import MapSetupService


class RegionBody(BaseModel, extra="forbid"):
    latitude: float = Field(strict=True, ge=-85.05112878, le=85.05112878)
    longitude: float = Field(strict=True, ge=-180, le=180)
    radius_km: float = Field(default=20, strict=True, ge=1, le=100)
    max_zoom: int = Field(default=14, strict=True, ge=7, le=15)


class PlanBody(BaseModel, extra="forbid"):
    region: RegionBody | None = None
    budget_bytes: int = Field(
        default=512 * 1024**2, strict=True, ge=64 * 1024**2, le=MAX_DOWNLOAD_BYTES
    )


def register_map_routes(
    app: FastAPI,
    service: MapSetupService | None,
    suggestion: Callable[[], dict[str, Any]] | None,
) -> None:
    def failure(error: str, status: int = 409) -> JSONResponse:
        return JSONResponse({"error": {"code": "map_setup", "message": error}}, status_code=status)

    @app.get("/api/v1/maps", response_model=None)
    async def status() -> JSONResponse:
        if service is None:
            return failure("Map setup is unavailable.", 503)
        value = await asyncio.to_thread(service.status)
        value["suggestion"] = suggestion() if suggestion else {"available": False}
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.post("/api/v1/maps/plan", response_model=None)
    async def plan(body: PlanBody) -> JSONResponse:
        if service is None:
            return failure("Map setup is unavailable.", 503)
        try:
            region = Region(**body.region.model_dump()) if body.region else None
            result = await asyncio.to_thread(service.plan, region, body.budget_bytes)
            return JSONResponse(result, status_code=202)
        except MapError as error:
            return failure(str(error))

    @app.post("/api/v1/maps/download/{identifier}", response_model=None)
    async def download(identifier: str) -> JSONResponse:
        if service is None:
            return failure("Map setup is unavailable.", 503)
        try:
            result = await asyncio.to_thread(service.download, identifier)
            return JSONResponse(result, status_code=202)
        except (MapError, OSError) as error:
            return failure(str(error) if isinstance(error, MapError) else "Prepare a new map plan.")

    @app.post("/api/v1/maps/pause", response_model=None)
    async def pause() -> JSONResponse:
        if service is None:
            return failure("Map setup is unavailable.", 503)
        service.pause()
        return JSONResponse({"state": "pausing"}, status_code=202)
