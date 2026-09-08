"""Two-phase operator control for the existing BBS history association."""

from __future__ import annotations

import hashlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from outpost.fed.adoption import (
    AdoptionActor,
    AdoptionConflict,
    AdoptionDenied,
    FederationAdoptionService,
)


class HistoryBody(BaseModel, extra="forbid"):
    old_mesh_id: str = Field(pattern=r"^![0-9a-f]{8}$")
    old_node_name: str | None = Field(default=None, max_length=80)


class AdoptBody(HistoryBody):
    review_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_namespace: bool = Field(strict=True)


def _actor(request: Request) -> AdoptionActor:
    session = getattr(request.state, "web_session", None)
    if session is None or session.role not in {"operator", "administrator"}:
        raise AdoptionDenied("A named operator account is required")
    return AdoptionActor(
        session.account_id,
        hashlib.sha256(request.cookies.get("outpost_session", "").encode()).hexdigest(),
    )


def register_adoption_routes(app: FastAPI, service: FederationAdoptionService) -> None:
    async def perform(mesh_id: str, body: HistoryBody, request: Request) -> JSONResponse:
        try:
            result = await service.review(
                _actor(request),
                body.old_mesh_id,
                mesh_id,
                body.old_node_name,
                expected_token=body.review_token if isinstance(body, AdoptBody) else None,
                confirm_namespace=body.confirm_namespace if isinstance(body, AdoptBody) else False,
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return JSONResponse(
                {"error": {"code": "adoption_failed", "message": str(error)}},
                status_code=403
                if isinstance(error, AdoptionDenied)
                else 409
                if isinstance(error, AdoptionConflict)
                else 400,
                headers={"Cache-Control": "no-store"},
            )

    @app.post("/api/v1/federation/peers/{mesh_id}/adopt-origin/preview")
    async def preview(mesh_id: str, body: HistoryBody, request: Request) -> JSONResponse:
        return await perform(mesh_id, body, request)

    @app.post("/api/v1/federation/peers/{mesh_id}/adopt-origin")
    async def adopt(mesh_id: str, body: AdoptBody, request: Request) -> JSONResponse:
        return await perform(mesh_id, body, request)
