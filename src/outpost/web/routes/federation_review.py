"""Version-bound federation review routes, using the existing domain transaction."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from outpost.fed.review import FederationReviewService, ReviewConflict, review_token
from outpost.operator_context import current_actor
from outpost.store import Database


class FederationInboxBody(BaseModel):
    state: Literal["imported", "rejected"]
    review_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="Rejected by operator", max_length=160)


def register_federation_review_routes(
    app: FastAPI,
    database: Database,
    federation_inbox_import: Callable[[int, str], Awaitable[str]] | None,
) -> None:
    @app.get("/api/v1/federation/inbox")
    async def federation_inbox(state: str = "pending") -> dict[str, Any]:
        rows = await database.read(
            "SELECT i.*,p.mesh_id,p.node_name FROM fed_inbox_item i "
            "JOIN fed_peer p ON p.id=i.peer_id WHERE i.state=? "
            "ORDER BY i.received_at DESC LIMIT 100",
            (state,),
        )
        items = []
        for row in rows:
            item = dict(row)
            item["review_token"] = review_token(item)
            item["payload"] = json.loads(item.pop("payload_json"))
            items.append(item)
        return {"items": items}

    @app.patch("/api/v1/federation/inbox/{item_id}", response_model=None)
    async def federation_inbox_reject(
        item_id: int, body: FederationInboxBody
    ) -> dict[str, str] | Response:
        try:
            if body.state == "imported":
                if federation_inbox_import is None:
                    raise ValueError("Import unavailable.")
                stream = await federation_inbox_import(item_id, body.review_token)
                return {"state": "imported", "stream": stream}
            await FederationReviewService(database).reject(
                item_id, body.review_token, current_actor(), body.reason, int(time.time())
            )
            return {"state": "rejected"}
        except (KeyError, TypeError, ValueError) as error:
            code = "review_conflict" if isinstance(error, ReviewConflict) else "review_failed"
            return JSONResponse({"error": {"code": code, "message": str(error)}}, status_code=409)
