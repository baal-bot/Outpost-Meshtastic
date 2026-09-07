"""Bounded raw-file upload; named operators and the shared CSRF/module gates."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from outpost.fed.bundle_format import MAX_FILE_BYTES
from outpost.fed.bundles import BundleActor, BundleDenied, FederationBundleService
from outpost.fed.review import ReviewConflict

UPLOAD_SECONDS = 30


class CommissionBody(BaseModel, extra="forbid"):
    identity: str = Field(pattern=r"^![0-9a-f]{8}$")


class ExportBody(BaseModel, extra="forbid"):
    destination: str = Field(pattern=r"^![0-9a-f]{8}$")
    stream: str = Field(min_length=1, max_length=80)
    after: int = Field(default=0, strict=True, ge=0, lt=2**63)
    public_labels: bool = Field(default=False, strict=True)
    precise_locations: bool = Field(default=False, strict=True)
    approve_public_content: bool = Field(default=False, strict=True)
    expected_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _actor(request: Request) -> BundleActor:
    session = getattr(request.state, "web_session", None)
    if session is None or session.role not in {"administrator", "operator"}:
        raise BundleDenied("Sign in with a named operator account")
    return BundleActor(
        session.account_id,
        hashlib.sha256(request.cookies.get("outpost_session", "").encode()).hexdigest(),
    )


def _error(error: ValueError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "bundle_failed", "message": str(error)}},
        status_code=403
        if isinstance(error, BundleDenied)
        else 409
        if isinstance(error, ReviewConflict)
        else 400,
        headers={"Cache-Control": "no-store"},
    )


async def _upload(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
        raise ValueError("Upload the raw bundle as application/octet-stream")
    declared = request.headers.get("content-length")
    if declared is not None and (
        not declared.isdecimal() or len(declared) > 9 or int(declared) > MAX_FILE_BYTES
    ):
        raise ValueError("Bundle exceeds the 192 KiB limit")
    data = bytearray()
    try:
        async with asyncio.timeout(UPLOAD_SECONDS):
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_FILE_BYTES:
                    raise ValueError("Bundle exceeds the 192 KiB limit")
                data.extend(chunk)
    except TimeoutError as error:
        raise ValueError(
            "Bundle upload exceeded the 30-second limit; no import was started"
        ) from error
    return bytes(data)


def register_bundle_routes(app: FastAPI, service: FederationBundleService) -> None:
    @app.get("/api/v1/federation/bundles")
    async def bundle_status(request: Request) -> JSONResponse:
        try:
            return JSONResponse(
                await service.status(_actor(request)), headers={"Cache-Control": "no-store"}
            )
        except ValueError as error:
            return _error(error)

    @app.post("/api/v1/federation/bundles/identity")
    async def bundle_commission(request: Request, body: CommissionBody) -> JSONResponse:
        try:
            await service.commission(_actor(request), body.identity)
            return JSONResponse({"state": "commissioned"}, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return _error(error)

    @app.post("/api/v1/federation/bundles/export", response_model=None)
    async def bundle_export(request: Request, body: ExportBody) -> Response:
        try:
            value = await service.export(_actor(request), **body.model_dump())
            if isinstance(value, bytes):
                return Response(
                    value,
                    media_type="application/octet-stream",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": 'attachment; filename="outpost-transfer.opb"',
                    },
                )
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return _error(error)

    @app.post("/api/v1/federation/bundles/import")
    async def bundle_import(request: Request) -> JSONResponse:
        try:
            actor = _actor(request)
            # Strict consent headers keep the untrusted raw file out of JSON/multipart parsers.
            options: dict[str, Any] = {}
            token = request.headers.get("x-bundle-review")
            if token is not None:
                import re

                if not re.fullmatch(r"[0-9a-f]{64}", token):
                    raise ValueError("Invalid bundle review token")
                options["expected_token"] = token
            for header, field in (
                ("x-bundle-public", "approve_public_content"),
                ("x-bundle-labels", "public_labels"),
                ("x-bundle-locations", "precise_locations"),
            ):
                value = request.headers.get(header, "false")
                if value not in {"true", "false"}:
                    raise ValueError("Bundle consent headers must be true or false")
                options[field] = value == "true"
            result = await service.receive(actor, await _upload(request), **options)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except ValueError as error:
            return _error(error)
