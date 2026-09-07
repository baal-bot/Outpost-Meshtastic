"""Readiness HTTP translation; SelfCheckService owns assessment and observations."""

from __future__ import annotations

import ipaddress
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from outpost.operator_context import current_actor_ref
from outpost.self_check import SelfCheckService


class ReadinessObservationBody(BaseModel, extra="forbid"):
    check: str = Field(min_length=1, max_length=32, pattern=r"^[a-z_]+$")
    outcome: Literal["pass", "fail"]
    observed_at: int = Field(strict=True, ge=0, le=253402300799)
    review_token: str = Field(pattern=r"^[a-f0-9]{64}$")


def register_readiness_routes(app: FastAPI, self_check: SelfCheckService) -> None:
    @app.post(
        "/api/v1/diagnostics/readiness",
        response_class=JSONResponse,
        response_model=None,
    )
    async def diagnostic_readiness(request: Request) -> dict[str, Any] | Response:
        host = request.client.host if request.client is not None else ""
        try:
            local_request = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local_request = False
        if not local_request:
            return JSONResponse(
                {"error": {"code": "loopback_required", "message": "Local access required."}},
                status_code=403,
            )
        return await self_check.run("diagnostics-cli")

    @app.get("/api/v1/readiness")
    async def readiness() -> dict[str, Any]:
        return await self_check.latest()

    @app.post("/api/v1/readiness/run")
    async def run_readiness() -> dict[str, Any]:
        return await self_check.run(f"dashboard:{current_actor_ref()}")

    @app.post("/api/v1/readiness/observations", response_model=None)
    async def readiness_observation(
        body: ReadinessObservationBody,
    ) -> dict[str, Any] | Response:
        try:
            return await self_check.record_observation(
                body.check,
                body.outcome,
                body.observed_at,
                body.review_token,
                current_actor_ref(),
            )
        except ValueError as error:
            return JSONResponse(
                {"error": {"code": "readiness_observation_conflict", "message": str(error)}},
                status_code=409,
            )
