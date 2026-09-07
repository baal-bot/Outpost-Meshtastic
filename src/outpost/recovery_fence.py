"""Durable, deliberately narrow workbench for an unqualified restored identity."""

from __future__ import annotations

from outpost.store import Database

READ_ROUTES = frozenset(
    {
        "/api/v1/health",
        "/api/v1/auth/session",
        "/api/v1/web/transport",
        "/api/v1/recovery/review",
    }
)
AUTH_ROUTES = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/password",
        "/api/v1/auth/step-up",
    }
)


class RecoveryFence:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.active = False
        self.digest: str | None = None

    async def load(self) -> None:
        rows = await self.database.read("SELECT bundle_digest FROM recovery_fence WHERE id=1")
        self.active = bool(rows)
        self.digest = str(rows[0]["bundle_digest"]) if rows else None

    def permits(self, method: str, path: str) -> bool:
        if not self.active:
            return True
        if path.startswith("/api/") or path.rstrip("/") == "/metrics":
            return (method == "GET" and path in READ_ROUTES) or (
                method == "POST" and path in AUTH_ROUTES
            )
        return method in {"GET", "HEAD"}

    async def review(self) -> dict[str, object]:
        counts = {}
        for table in ("member", "post", "mail", "incident", "checkin", "fed_peer", "outbound_work"):
            # Fixed product-owned names, never input from the bundle or request.
            rows = await self.database.read(f"SELECT count(*) count FROM {table}")  # noqa: S608
            counts[table] = rows[0]["count"]
        identity = await self.database.read("SELECT mesh_id FROM fed_bundle_identity WHERE id=1")
        key = await self.database.read(
            "SELECT hex(public_key) key FROM fed_relay_identity WHERE id=1"
        )
        return {
            "state": "review_required" if self.active else "not_restored",
            "bundle_digest": self.digest,
            "radio_and_background_work": "disabled" if self.active else "normal",
            "counts": counts,
            "recorded_mesh_id": identity[0]["mesh_id"] if identity else None,
            "recorded_signing_public_key": key[0]["key"].lower() if key else None,
            "identity_continuity": "evidence_only_not_permission_to_transmit",
        }
