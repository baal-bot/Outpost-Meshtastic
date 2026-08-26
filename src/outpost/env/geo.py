from __future__ import annotations

import json
import math
import re
from typing import Any

from outpost.clock import Clock
from outpost.env.seismic import SeismicService
from outpost.store import Database


class WaypointService:
    def __init__(self, database: Database, clock: Clock) -> None:
        self.database, self.clock = database, clock

    @staticmethod
    def slug(name: str) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
        if not value:
            raise ValueError("Waypoint name must contain a letter or number.")
        return value[:48]

    async def list(self) -> list[dict[str, Any]]:
        rows = await self.database.read("SELECT * FROM waypoint ORDER BY name COLLATE NOCASE")
        return [dict(row) for row in rows]

    async def by_token(self, token: str) -> dict[str, Any] | None:
        rows = await self.database.read(
            "SELECT * FROM waypoint WHERE id=? OR slug=? COLLATE NOCASE LIMIT 1",
            (int(token) if token.isdigit() else -1, self.slug(token)),
        )
        return dict(rows[0]) if rows else None

    async def create(
        self, name: str, latitude: float, longitude: float, category: str, notes: str
    ) -> dict[str, Any]:
        self._validate(latitude, longitude)
        clean_name = name.strip()[:80]
        if not clean_name:
            raise ValueError("Waypoint name is required.")
        now = int(self.clock.now().timestamp())
        try:
            waypoint_id = await self.database.write(
                "INSERT INTO waypoint(name,slug,latitude,longitude,category,notes,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    clean_name,
                    self.slug(clean_name),
                    latitude,
                    longitude,
                    category[:32] or "general",
                    notes.strip()[:500] or None,
                    now,
                    now,
                ),
            )
        except Exception as error:
            if "UNIQUE" in str(error):
                raise ValueError("A waypoint with that name already exists.") from error
            raise
        return await self.get(waypoint_id)

    async def get(self, waypoint_id: int) -> dict[str, Any]:
        rows = await self.database.read("SELECT * FROM waypoint WHERE id=?", (waypoint_id,))
        if not rows:
            raise ValueError("Waypoint not found.")
        return dict(rows[0])

    async def update(self, waypoint_id: int, values: dict[str, Any]) -> dict[str, Any]:
        current = await self.get(waypoint_id)
        latitude = float(values.get("latitude", current["latitude"]))
        longitude = float(values.get("longitude", current["longitude"]))
        self._validate(latitude, longitude)
        name = str(values.get("name", current["name"])).strip()[:80]
        if not name:
            raise ValueError("Waypoint name is required.")
        await self.database.write(
            "UPDATE waypoint SET name=?,slug=?,latitude=?,longitude=?,category=?,"
            "notes=?,updated_at=? WHERE id=?",
            (
                name,
                self.slug(name),
                latitude,
                longitude,
                str(values.get("category", current["category"]))[:32] or "general",
                str(values.get("notes", current["notes"] or "")).strip()[:500] or None,
                int(self.clock.now().timestamp()),
                waypoint_id,
            ),
        )
        return await self.get(waypoint_id)

    async def delete(self, waypoint_id: int) -> None:
        await self.get(waypoint_id)
        await self.database.write("DELETE FROM waypoint WHERE id=?", (waypoint_id,))

    async def member_position(
        self, member_id: int | None = None, handle: str | None = None
    ) -> dict[str, Any] | None:
        query = (
            """SELECT m.id,m.mesh_id,m.handle,m.prefs,p.lat,p.lon,p.received_at,p.expires_at
               FROM member m JOIN member_position p ON p.member_id=m.id
               WHERE m.id=? AND p.expires_at>? LIMIT 1"""
            if member_id is not None
            else """SELECT m.id,m.mesh_id,m.handle,m.prefs,p.lat,p.lon,p.received_at,p.expires_at
                     FROM member m JOIN member_position p ON p.member_id=m.id
                     WHERE m.handle=? AND p.expires_at>? LIMIT 1"""
        )
        value = member_id if member_id is not None else handle
        rows = await self.database.read(
            query,
            (value, int(self.clock.now().timestamp())),
        )
        return dict(rows[0]) if rows else None

    async def set_position_privacy(self, member_id: int, preference: str) -> str:
        value = preference.lower()
        if value not in {"full", "coarse", "off"}:
            raise ValueError("Position sharing must be full, coarse, or off.")
        await self.database.write(
            "UPDATE member SET prefs=json_set(COALESCE(prefs,'{}'),'$.position',?) WHERE id=?",
            (value, member_id),
        )
        return value

    async def position_privacy(self, member_id: int) -> str:
        rows = await self.database.read(
            "SELECT json_extract(prefs,'$.position') AS preference FROM member WHERE id=?",
            (member_id,),
        )
        return str(rows[0]["preference"] or "coarse") if rows else "coarse"

    @staticmethod
    def privacy_position(
        value: dict[str, Any], coarse_precision_m: int, operator: bool = False
    ) -> tuple[float, float] | None:
        try:
            preference = json.loads(value.get("prefs") or "{}").get("position", "coarse")
        except (TypeError, json.JSONDecodeError):
            preference = "coarse"
        if preference == "off":
            return None
        latitude, longitude = float(value["lat"]), float(value["lon"])
        if preference == "full" or operator:
            return latitude, longitude
        lat_step = coarse_precision_m / 111_320
        lon_step = coarse_precision_m / max(1, 111_320 * math.cos(math.radians(latitude)))
        return round(latitude / lat_step) * lat_step, round(longitude / lon_step) * lon_step

    @staticmethod
    def _validate(latitude: float, longitude: float) -> None:
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("Waypoint coordinates are outside valid bounds.")

    @staticmethod
    def distance_bearing(
        origin_lat: float, origin_lon: float, waypoint: dict[str, Any]
    ) -> tuple[float, int]:
        return SeismicService.distance_bearing(
            origin_lat, origin_lon, float(waypoint["latitude"]), float(waypoint["longitude"])
        )
