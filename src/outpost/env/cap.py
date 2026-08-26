from __future__ import annotations

import json
import urllib.error
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from outpost.clock import Clock
from outpost.config import EnvConfig
from outpost.env.weather import NWS_HOST, _request_json
from outpost.store import Database
from outpost.watch import AlertService

CAP_POINT_FRESH_SECONDS = 300
CAP_POINT_STALE_SECONDS = 1_800


class CapAlertService:
    def __init__(self, database: Database, clock: Clock, config: EnvConfig) -> None:
        self.database, self.clock, self.config = database, clock, config
        self.last_poll_at: int | None = None
        self.last_error: str | None = None

    @staticmethod
    def _point_in_ring(lat: float, lon: float, ring: list[list[float]]) -> bool:
        inside = False
        for index, point in enumerate(ring):
            prior = ring[index - 1]
            x1, y1 = prior[0], prior[1]
            x2, y2 = point[0], point[1]
            if (y1 > lat) != (y2 > lat):
                crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
                if lon < crossing:
                    inside = not inside
        return inside

    @classmethod
    def _geometry_contains(cls, geometry: dict[str, Any], lat: float, lon: float) -> bool:
        coordinates = geometry.get("coordinates") or []
        polygons = [coordinates] if geometry.get("type") == "Polygon" else coordinates
        return any(polygon and cls._point_in_ring(lat, lon, polygon[0]) for polygon in polygons)

    @classmethod
    def _gate(
        cls,
        properties: dict[str, Any],
        now: datetime,
        geometry: dict[str, Any] | None = None,
        point: tuple[float, float] | None = None,
    ) -> tuple[str, list[str]]:
        reasons: list[str] = []
        if properties.get("status") != "Actual":
            reasons.append("status is not Actual")
        if properties.get("severity") not in {"Extreme", "Severe"}:
            reasons.append("severity is below Severe")
        if properties.get("urgency") not in {"Immediate", "Expected"}:
            reasons.append("urgency is not Immediate or Expected")
        if properties.get("certainty") == "Unlikely":
            reasons.append("certainty is Unlikely")
        try:
            expires = datetime.fromisoformat(str(properties["expires"]).replace("Z", "+00:00"))
            if expires <= now:
                reasons.append("alert is expired")
        except (KeyError, TypeError, ValueError):
            reasons.append("expiry is missing or invalid")
        msg_type = str(properties.get("messageType") or properties.get("msgType") or "Alert")
        if msg_type not in {"Alert", "Update", "Cancel"}:
            reasons.append(f"message type {msg_type} is log-only")
        if geometry and point and not cls._geometry_contains(geometry, point[0], point[1]):
            reasons.append("alert polygon does not contain the Outpost")
        return ("withheld" if reasons else "accepted", reasons)

    @staticmethod
    def _normalized_point(lat: float, lon: float) -> tuple[float, float, str]:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("invalid alert coordinates")
        query_lat, query_lon = round(lat, 4), round(lon, 4)
        if query_lat == 0:
            query_lat = 0.0
        if query_lon == 0:
            query_lon = 0.0
        return query_lat, query_lon, f"nws-cap:{query_lat:.4f},{query_lon:.4f}"

    @staticmethod
    def _service_area(point: dict[str, Any]) -> str | None:
        properties = point.get("properties")
        if not isinstance(properties, dict):
            return None
        relative = properties.get("relativeLocation")
        relative_properties = relative.get("properties") if isinstance(relative, dict) else None
        if isinstance(relative_properties, dict):
            city = str(relative_properties.get("city") or "").strip()
            state = str(relative_properties.get("state") or "").strip()
            if city or state:
                return ", ".join(value for value in (city, state) if value)
        zone = str(properties.get("forecastZone") or "").rstrip("/").rsplit("/", 1)[-1]
        return zone or None

    @staticmethod
    def _peer_items(payload: dict[str, Any]) -> list[dict[str, object]]:
        features = payload.get("features")
        if not isinstance(features, list):
            raise ValueError("NWS alert response is missing its feature list")
        items: list[dict[str, object]] = []
        for feature in features[:5]:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            if not isinstance(properties, dict) or properties.get("status") != "Actual":
                continue
            items.append(
                {
                    "event": str(properties.get("event") or "Public alert")[:80],
                    "headline": str(
                        properties.get("headline") or properties.get("event") or "Public alert"
                    )[:180],
                    "severity": str(properties.get("severity") or "Unknown")[:20],
                    "area_desc": str(properties.get("areaDesc") or "")[:120],
                    "expires_at": str(properties.get("expires") or "")[:40],
                }
            )
        return items

    @staticmethod
    def _point_response(
        row: dict[str, Any], now: int, *, status: str | None = None, error: str | None = None
    ) -> tuple[dict[str, object], dict[str, object]]:
        fetched_at = int(row["fetched_at"])
        active_items: list[dict[str, object]] = []
        for item in json.loads(row["result_json"]):
            try:
                expiry = datetime.fromisoformat(
                    str(item["expires_at"]).replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            if expiry > now:
                active_items.append(item)
        result_status = status or str(row["status"])
        if result_status == "ok" and not active_items:
            result_status = "empty"
        result: dict[str, object] = {
            "status": result_status,
            "items": active_items,
        }
        if error:
            result["error"] = error
        return result, {
            "provider": str(row["provider"]),
            "query_lat": float(row["query_lat"]),
            "query_lon": float(row["query_lon"]),
            "service_area": row.get("service_area"),
            "provider_timestamp": row.get("provider_timestamp"),
            "fetched_at": fetched_at,
            "cache_age_seconds": max(0, now - fetched_at),
        }

    def _point_failure(
        self,
        cached: dict[str, Any] | None,
        query_lat: float,
        query_lon: float,
        now: int,
        error: Exception,
    ) -> tuple[dict[str, object], dict[str, object]]:
        detail = str(error)[:160] or type(error).__name__
        if (
            cached is not None
            and cached["status"] in {"ok", "empty"}
            and now - int(cached["fetched_at"]) <= CAP_POINT_STALE_SECONDS
        ):
            return self._point_response(cached, now, status="stale", error=detail)
        return {
            "status": "provider_failure",
            "items": [],
            "error": detail,
        }, {
            "provider": "NWS CAP",
            "query_lat": query_lat,
            "query_lon": query_lon,
            "service_area": None,
            "provider_timestamp": None,
            "fetched_at": None,
            "cache_age_seconds": None,
        }

    async def query_point(
        self, lat: float, lon: float
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Return alerts for one normalized point without leaking the local CAP inbox."""
        query_lat, query_lon, cache_key = self._normalized_point(lat, lon)
        now = int(self.clock.now().timestamp())
        rows = await self.database.read(
            "SELECT * FROM cap_point_cache WHERE cache_key=?", (cache_key,)
        )
        cached = dict(rows[0]) if rows else None
        if cached is not None and now - int(cached["fetched_at"]) < CAP_POINT_FRESH_SECONDS:
            return self._point_response(cached, now)

        try:
            point = await _request_json(
                f"https://{NWS_HOST}/points/{query_lat:.4f},{query_lon:.4f}",
                NWS_HOST,
                self.config,
            )
        except urllib.error.HTTPError as error:
            if error.code != 404:
                return self._point_failure(cached, query_lat, query_lon, now, error)
            row = {
                "provider": "NWS CAP",
                "query_lat": query_lat,
                "query_lon": query_lon,
                "service_area": None,
                "status": "unsupported_region",
                "result_json": "[]",
                "provider_timestamp": None,
                "fetched_at": now,
            }
            await self.database.write(
                "INSERT INTO cap_point_cache(cache_key,provider,query_lat,query_lon,service_area,"
                "status,result_json,provider_timestamp,fetched_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET provider=excluded.provider,"
                "query_lat=excluded.query_lat,query_lon=excluded.query_lon,service_area=NULL,"
                "status=excluded.status,result_json=excluded.result_json,provider_timestamp=NULL,"
                "fetched_at=excluded.fetched_at",
                (
                    cache_key,
                    row["provider"],
                    query_lat,
                    query_lon,
                    None,
                    row["status"],
                    row["result_json"],
                    None,
                    now,
                ),
            )
            return self._point_response(row, now)
        except Exception as error:  # Provider failures are returned as an explicit result state.
            return self._point_failure(cached, query_lat, query_lon, now, error)

        service_area = self._service_area(point)
        query = urllib.parse.urlencode(
            {"point": f"{query_lat:.4f},{query_lon:.4f}", "status": "actual"}
        )
        try:
            payload = await _request_json(
                f"https://{NWS_HOST}/alerts/active?{query}", NWS_HOST, self.config
            )
            items = self._peer_items(payload)
        except Exception as error:  # Provider failures are returned as an explicit result state.
            return self._point_failure(cached, query_lat, query_lon, now, error)

        status = "ok" if items else "empty"
        provider_timestamp = str(payload.get("updated") or "")[:40] or None
        encoded = json.dumps(items, separators=(",", ":"))
        await self.database.write(
            "INSERT INTO cap_point_cache(cache_key,provider,query_lat,query_lon,service_area,"
            "status,result_json,provider_timestamp,fetched_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET provider=excluded.provider,"
            "query_lat=excluded.query_lat,query_lon=excluded.query_lon,"
            "service_area=excluded.service_area,status=excluded.status,"
            "result_json=excluded.result_json,provider_timestamp=excluded.provider_timestamp,"
            "fetched_at=excluded.fetched_at",
            (
                cache_key,
                "NWS CAP",
                query_lat,
                query_lon,
                service_area,
                status,
                encoded,
                provider_timestamp,
                now,
            ),
        )
        return self._point_response(
            {
                "provider": "NWS CAP",
                "query_lat": query_lat,
                "query_lon": query_lon,
                "service_area": service_area,
                "status": status,
                "result_json": encoded,
                "provider_timestamp": provider_timestamp,
                "fetched_at": now,
            },
            now,
        )

    async def poll(self, lat: float, lon: float) -> dict[str, int]:
        query = urllib.parse.urlencode({"point": f"{lat:.4f},{lon:.4f}", "status": "actual"})
        try:
            payload = await _request_json(
                f"https://{NWS_HOST}/alerts/active?{query}", NWS_HOST, self.config
            )
        except Exception as error:  # Provider failures must never stop the scheduler.
            self.last_error = str(error)[:160]
            raise OSError(self.last_error) from error
        now_dt = self.clock.now().astimezone(UTC)
        now = int(now_dt.timestamp())
        counts = {"seen": 0, "accepted": 0, "withheld": 0}
        for feature in payload.get("features", []):
            properties = feature.get("properties") or {}
            identifier = str(properties.get("id") or feature.get("id") or "").strip()
            if not identifier:
                continue
            decision, reasons = self._gate(properties, now_dt, feature.get("geometry"), (lat, lon))
            msg_type = str(properties.get("messageType") or properties.get("msgType") or "Alert")
            values = (
                identifier,
                properties.get("sender"),
                properties.get("sent"),
                msg_type,
                str(properties.get("status") or "Unknown"),
                str(properties.get("event") or "Public alert"),
                str(properties.get("headline") or properties.get("event") or "Public alert"),
                properties.get("description"),
                properties.get("areaDesc"),
                properties.get("severity"),
                properties.get("urgency"),
                properties.get("certainty"),
                properties.get("effective"),
                str(properties.get("expires") or now_dt.isoformat()),
                properties.get("references"),
                decision,
                json.dumps(reasons, separators=(",", ":")),
                json.dumps(feature, separators=(",", ":")),
                now,
                now,
            )
            await self.database.write(
                """INSERT INTO cap_alert(identifier,sender,sent_at,msg_type,status,event,headline,
                   description,area_desc,severity,urgency,certainty,effective_at,expires_at,
                   references_text,decision,gate_reasons,raw_json,first_seen_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(identifier) DO UPDATE SET
                   sender=excluded.sender,sent_at=excluded.sent_at,
                   msg_type=excluded.msg_type,status=excluded.status,event=excluded.event,
                   headline=excluded.headline,description=excluded.description,
                   area_desc=excluded.area_desc,severity=excluded.severity,urgency=excluded.urgency,
                   certainty=excluded.certainty,effective_at=excluded.effective_at,
                   expires_at=excluded.expires_at,references_text=excluded.references_text,
                   decision=excluded.decision,gate_reasons=excluded.gate_reasons,
                   raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                values,
            )
            counts["seen"] += 1
            counts[decision] += 1
        await self.database.write(
            "UPDATE cap_alert SET review_state='expired' "
            "WHERE review_state='pending' AND expires_at<=?",
            (now_dt.isoformat(),),
        )
        self.last_poll_at, self.last_error = now, None
        return counts

    async def list(self, *, include_expired: bool = False) -> list[dict[str, Any]]:
        where = "" if include_expired else "WHERE review_state!='expired'"
        rows = await self.database.read(
            f"SELECT * FROM cap_alert {where} ORDER BY updated_at DESC LIMIT 100"  # noqa: S608
        )
        values = []
        for row in rows:
            value = dict(row)
            value["gate_reasons"] = json.loads(value["gate_reasons"])
            value.pop("raw_json", None)
            values.append(value)
        return values

    async def dismiss(self, cap_id: int) -> None:
        rows = await self.database.read(
            "SELECT id FROM cap_alert WHERE id=? AND review_state='pending'", (cap_id,)
        )
        if not rows:
            raise ValueError("CAP alert is not pending review.")
        await self.database.write(
            "UPDATE cap_alert SET review_state='dismissed',updated_at=unixepoch() "
            "WHERE id=? AND review_state='pending'",
            (cap_id,),
        )

    async def approve(self, cap_id: int, alerts: AlertService) -> dict[str, Any]:
        rows = await self.database.read(
            "SELECT * FROM cap_alert WHERE id=? AND decision='accepted' AND review_state='pending'",
            (cap_id,),
        )
        if not rows:
            raise ValueError("CAP alert is not eligible for approval.")
        item = rows[0]
        previous = await self._referenced_approved(str(item["references_text"] or ""))
        if item["msg_type"] in {"Update", "Cancel"} and previous is None:
            raise ValueError("No approved referenced alert was found.")
        if item["msg_type"] == "Cancel":
            assert previous is not None
            value = await alerts.cancel(
                int(previous["linked_alert_id"]), f"NWS cancelled {item['event']}", "web:operator"
            )
            await self.database.write(
                "UPDATE cap_alert SET review_state='approved',linked_alert_id=?,"
                "updated_at=unixepoch() WHERE id=?",
                (value.id, cap_id),
            )
            return value.json()
        expiry = datetime.fromisoformat(str(item["expires_at"]).replace("Z", "+00:00"))
        headline = (
            f"NWS {item['event']} · {item['area_desc'] or 'local area'} · until {expiry:%H:%M}"
        )
        while len(headline.encode()) > 140:
            headline = headline[:-1]
        value = await alerts.raise_alert(
            "critical" if item["severity"] == "Extreme" else "urgent",
            headline,
            "web:operator",
            source="cap",
            supersedes_alert_id=(
                int(previous["linked_alert_id"])
                if item["msg_type"] == "Update" and previous is not None
                else None
            ),
        )
        await self.database.write(
            "UPDATE cap_alert SET review_state='approved',linked_alert_id=?,"
            "updated_at=unixepoch() WHERE id=?",
            (value.id, cap_id),
        )
        return value.json()

    async def _referenced_approved(self, references: str) -> dict[str, Any] | None:
        identifiers = {
            fields[1] for token in references.split() if len(fields := token.split(",")) >= 2
        }
        if not identifiers:
            return None
        rows = await self.database.read(
            "SELECT * FROM cap_alert WHERE review_state='approved' "
            "AND linked_alert_id IS NOT NULL ORDER BY updated_at DESC"
        )
        return next((dict(row) for row in rows if row["identifier"] in identifiers), None)

    def health(self) -> dict[str, Any]:
        return {"last_poll_at": self.last_poll_at, "last_error": self.last_error}
