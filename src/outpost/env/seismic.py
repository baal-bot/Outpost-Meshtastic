from __future__ import annotations

import math
from typing import Any

from outpost.clock import Clock
from outpost.config import EnvConfig
from outpost.env.weather import _request_json
from outpost.store import Database
from outpost.watch import AlertService

USGS_HOST = "earthquake.usgs.gov"
USGS_FEED = f"https://{USGS_HOST}/earthquakes/feed/v1.0/summary/all_hour.geojson"


class SeismicService:
    def __init__(self, database: Database, clock: Clock, config: EnvConfig) -> None:
        self.database, self.clock, self.config = database, clock, config
        self.last_poll_at: int | None = None
        self.last_error: str | None = None

    @staticmethod
    def distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, int]:
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        a = (
            math.sin(delta_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lon / 2) ** 2
        )
        distance = 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        y = math.sin(delta_lon) * math.cos(phi2)
        x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lon)
        return distance, round((math.degrees(math.atan2(y, x)) + 360) % 360)

    async def poll(self, lat: float, lon: float) -> dict[str, int]:
        try:
            payload = await _request_json(USGS_FEED, USGS_HOST, self.config)
        except Exception as error:
            self.last_error = str(error)[:160]
            raise OSError(self.last_error) from error
        now = int(self.clock.now().timestamp())
        counts = {"seen": 0, "nearby": 0, "updated": 0, "review": 0}
        for feature in payload.get("features", []):
            properties, geometry = feature.get("properties") or {}, feature.get("geometry") or {}
            coordinates = geometry.get("coordinates") or []
            if len(coordinates) < 3 or properties.get("mag") is None:
                continue
            counts["seen"] += 1
            quake_lon, quake_lat, depth = map(float, coordinates[:3])
            distance, bearing = self.distance_bearing(lat, lon, quake_lat, quake_lon)
            if distance > self.config.earthquake_radius_km:
                continue
            counts["nearby"] += 1
            usgs_id = str(feature.get("id") or "").strip()
            if not usgs_id:
                continue
            magnitude = float(properties["mag"])
            significant = magnitude >= self.config.earthquake_review_magnitude
            previous = await self.database.read(
                "SELECT source_updated_at,review_state FROM earthquake WHERE usgs_id=?", (usgs_id,)
            )
            source_updated = int(properties.get("updated") or properties.get("time") or 0) // 1000
            if previous and int(previous[0]["source_updated_at"]) == source_updated:
                continue
            counts["updated"] += int(bool(previous))
            review_state = (
                str(previous[0]["review_state"])
                if previous and previous[0]["review_state"] in {"approved", "dismissed"}
                else ("pending" if significant else "observed")
            )
            counts["review"] += int(review_state == "pending")
            await self.database.write(
                """INSERT INTO earthquake(usgs_id,magnitude,place,occurred_at,source_updated_at,
                   longitude,latitude,depth_km,distance_km,bearing_deg,significance,usgs_url,
                   review_state,first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(usgs_id) DO UPDATE SET magnitude=excluded.magnitude,
                   place=excluded.place,occurred_at=excluded.occurred_at,
                   source_updated_at=excluded.source_updated_at,longitude=excluded.longitude,
                   latitude=excluded.latitude,depth_km=excluded.depth_km,
                   distance_km=excluded.distance_km,bearing_deg=excluded.bearing_deg,
                   significance=excluded.significance,usgs_url=excluded.usgs_url,
                   review_state=excluded.review_state,updated_at=excluded.updated_at""",
                (
                    usgs_id,
                    magnitude,
                    str(properties.get("place") or "Unknown location"),
                    int(properties.get("time") or 0) // 1000,
                    source_updated,
                    quake_lon,
                    quake_lat,
                    depth,
                    round(distance, 1),
                    bearing,
                    int(significant),
                    properties.get("url"),
                    review_state,
                    now,
                    now,
                ),
            )
        self.last_poll_at, self.last_error = now, None
        return counts

    async def list(self, hours: int = 24) -> list[dict[str, Any]]:
        cutoff = int(self.clock.now().timestamp()) - hours * 3600
        rows = await self.database.read(
            "SELECT * FROM earthquake WHERE occurred_at>=? ORDER BY occurred_at DESC LIMIT 100",
            (cutoff,),
        )
        return [dict(row) for row in rows]

    async def dismiss(self, quake_id: int) -> None:
        rows = await self.database.read(
            "SELECT id FROM earthquake WHERE id=? AND review_state='pending'", (quake_id,)
        )
        if not rows:
            raise ValueError("Earthquake is not pending review.")
        await self.database.write(
            "UPDATE earthquake SET review_state='dismissed',updated_at=unixepoch() WHERE id=?",
            (quake_id,),
        )

    async def approve(self, quake_id: int, alerts: AlertService) -> dict[str, Any]:
        rows = await self.database.read(
            "SELECT * FROM earthquake WHERE id=? AND review_state='pending'", (quake_id,)
        )
        if not rows:
            raise ValueError("Earthquake is not eligible for approval.")
        quake = rows[0]
        headline = (
            f"USGS M{quake['magnitude']:.1f} earthquake · {quake['distance_km']:.0f}km "
            f"at {quake['bearing_deg']}° · {quake['place']}"
        )
        while len(headline.encode()) > 140:
            headline = headline[:-1]
        alert = await alerts.raise_alert(
            "urgent" if quake["magnitude"] < 6 else "critical",
            headline,
            "web:operator",
            source="operator",
            lat=float(quake["latitude"]),
            lon=float(quake["longitude"]),
            radius_km=min(100.0, max(1.0, float(quake["distance_km"]) / 4)),
        )
        await self.database.write(
            "UPDATE earthquake SET review_state='approved',linked_alert_id=?,"
            "updated_at=unixepoch() WHERE id=?",
            (alert.id, quake_id),
        )
        return alert.json()

    def health(self) -> dict[str, Any]:
        return {"last_poll_at": self.last_poll_at, "last_error": self.last_error}
