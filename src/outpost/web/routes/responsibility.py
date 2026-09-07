"""Operator-only responsibility views; the domain owns current-role/CAS transactions."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from outpost.watch.responsibility import (
    Action,
    IncidentResponsibilityService,
    ResponsibilityActor,
    ResponsibilityConflict,
    ResponsibilityDenied,
    TargetKind,
)


class ResponsibilityBody(BaseModel, extra="forbid"):
    action: Action
    review_token: str = Field(pattern=r"^[a-f0-9]{24}$")
    target_kind: TargetKind | None = None
    target_ref: int | None = Field(default=None, strict=True, ge=1, lt=2**63)
    next_action: str | None = Field(default=None, max_length=160)


def _actor(request: Request) -> ResponsibilityActor:
    session = getattr(request.state, "web_session", None)
    if session is None or session.role not in {"operator", "administrator"}:
        raise ResponsibilityDenied("Sign in with a named operator account.")
    cookie = request.cookies.get("outpost_session", "")
    return ResponsibilityActor(
        account_id=session.account_id, session_hash=hashlib.sha256(cookie.encode()).hexdigest()
    )


def _error(error: ValueError) -> JSONResponse:
    status = (
        403
        if isinstance(error, ResponsibilityDenied)
        else 409
        if isinstance(error, ResponsibilityConflict)
        else 400
    )
    return JSONResponse(
        {"error": {"code": "responsibility_failed", "message": str(error)}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def register_responsibility_routes(app: FastAPI, service: IncidentResponsibilityService) -> None:
    @app.get("/api/v1/incidents/responsibility/targets", response_model=None)
    async def responsibility_targets(
        request: Request,
        kind: TargetKind,
        after: int = Query(0, ge=0, lt=2**63),
        query: str = Query("", max_length=50),
    ) -> JSONResponse:
        try:
            _actor(request)
            return JSONResponse(
                {"items": await service.targets(kind, after=after, query=query)},
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as error:
            return _error(error)

    @app.get("/api/v1/incidents/{incident_id}/responsibility", response_model=None)
    async def responsibility_get(request: Request, incident_id: int) -> JSONResponse:
        try:
            value = await service.snapshot(incident_id, _actor(request))
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return _error(error)

    @app.get("/api/v1/incidents/{incident_id}/responsibility/history", response_model=None)
    async def responsibility_history(
        request: Request,
        incident_id: int,
        after: int = Query(0, ge=0, lt=2**63),
    ) -> JSONResponse:
        try:
            await service.snapshot(incident_id, _actor(request))
            return JSONResponse(
                {"items": await service.history(incident_id, after=after)},
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as error:
            return _error(error)

    @app.post("/api/v1/incidents/{incident_id}/responsibility", response_model=None)
    async def responsibility_apply(
        request: Request,
        incident_id: int,
        body: ResponsibilityBody,
    ) -> JSONResponse:
        try:
            actor = _actor(request)
            result: dict[str, Any] = await service.apply(
                incident_id,
                actor,
                body.action,
                body.review_token,
                target_kind=body.target_kind,
                target_ref=body.target_ref,
                next_action=body.next_action,
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return _error(error)
