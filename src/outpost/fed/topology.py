from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from outpost.audit import write_audit
from outpost.clock import Clock
from outpost.fed.peers import FederationPeerService
from outpost.store import Database

TOPOLOGY_INTERVAL_SECONDS = 6 * 3_600
TOPOLOGY_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True)
class TopologyPolicy:
    peer_id: int
    mesh_id: str
    share_location: bool
    location_lat: float | None
    location_lon: float | None
    precision_km: float
    updated_at: int | None
    updated_by: str | None
    last_sent_at: int | None

    def json(self) -> dict[str, Any]:
        return asdict(self)


class FederationTopologyService:
    """Privacy-gated peer location exchange and safe topology health projection."""

    def __init__(self, database: Database, peers: FederationPeerService, clock: Clock) -> None:
        self.database, self.peers, self.clock = database, peers, clock

    @staticmethod
    def _policy(row: Any) -> TopologyPolicy:
        return TopologyPolicy(
            peer_id=int(row["id"]),
            mesh_id=str(row["mesh_id"]),
            share_location=bool(row["share_location"] or 0),
            location_lat=(float(row["policy_lat"]) if row["policy_lat"] is not None else None),
            location_lon=(float(row["policy_lon"]) if row["policy_lon"] is not None else None),
            precision_km=float(row["precision_km"] or 10),
            updated_at=int(row["updated_at"]) if row["updated_at"] is not None else None,
            updated_by=str(row["updated_by"]) if row["updated_by"] is not None else None,
            last_sent_at=(int(row["last_sent_at"]) if row["last_sent_at"] is not None else None),
        )

    async def policy(self, mesh_id: str) -> TopologyPolicy:
        rows = await self.database.read(
            "SELECT p.id,p.mesh_id,t.share_location,t.location_lat policy_lat,"
            "t.location_lon policy_lon,t.precision_km,t.updated_at,t.updated_by,t.last_sent_at "
            "FROM fed_peer p LEFT JOIN fed_topology_policy t ON t.peer_id=p.id "
            "WHERE p.mesh_id=?",
            (mesh_id,),
        )
        if not rows:
            raise ValueError("federation peer not found")
        return self._policy(rows[0])

    async def set_policy(
        self,
        mesh_id: str,
        *,
        share_location: bool,
        location_lat: float | None,
        location_lon: float | None,
        precision_km: float,
        actor: str,
    ) -> TopologyPolicy:
        peer = await self.peers.by_mesh_id(mesh_id)
        if peer.state not in {"active", "paused"}:
            raise ValueError("topology sharing requires a paired peer")
        if share_location and (location_lat is None or location_lon is None):
            raise ValueError("shared topology location needs latitude and longitude")
        if (location_lat is not None and not -90 <= location_lat <= 90) or (
            location_lon is not None and not -180 <= location_lon <= 180
        ):
            raise ValueError("topology location is outside coordinate bounds")
        if not 1 <= precision_km <= 100:
            raise ValueError("topology precision must be 1-100 km")
        now = int(self.clock.now().timestamp())
        detail = {
            "share_location": share_location,
            "precision_km": precision_km,
            "coordinates_configured": location_lat is not None and location_lon is not None,
        }
        async with self.database.transaction() as transaction:
            await transaction.write(
                "INSERT INTO fed_topology_policy(peer_id,share_location,location_lat,"
                "location_lon,precision_km,updated_at,updated_by,last_sent_at) "
                "VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(peer_id) DO UPDATE SET "
                "share_location=excluded.share_location,location_lat=excluded.location_lat,"
                "location_lon=excluded.location_lon,precision_km=excluded.precision_km,"
                "updated_at=excluded.updated_at,updated_by=excluded.updated_by,last_sent_at=NULL",
                (
                    peer.id,
                    int(share_location),
                    location_lat,
                    location_lon,
                    precision_km,
                    now,
                    actor[:160],
                ),
            )
            await write_audit(
                transaction,
                actor_kind="web",
                actor_ref=actor,
                action="federation.topology_policy",
                target=mesh_id,
                detail=detail,
                created_at=now,
            )
        return await self.policy(mesh_id)

    @staticmethod
    def _coarsen(latitude: float, longitude: float, precision_km: float) -> tuple[float, float]:
        latitude_step = max(precision_km / 111.0, 0.009)
        longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.2)
        longitude_step = max(precision_km / (111.0 * longitude_scale), 0.009)
        return (
            max(-90.0, min(90.0, round(latitude / latitude_step) * latitude_step)),
            max(-180.0, min(180.0, round(longitude / longitude_step) * longitude_step)),
        )

    async def advertisement(self, mesh_id: str) -> dict[str, Any]:
        policy = await self.policy(mesh_id)
        now = int(self.clock.now().timestamp())
        location = None
        if (
            policy.share_location
            and policy.location_lat is not None
            and policy.location_lon is not None
        ):
            latitude, longitude = self._coarsen(
                policy.location_lat, policy.location_lon, policy.precision_km
            )
            location = {
                "lat": latitude,
                "lon": longitude,
                "precision_km": policy.precision_km,
            }
        return {"generated_at": now, "location": location}

    async def due(self, *, now: int | None = None) -> list[str]:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        rows = await self.database.read(
            "SELECT p.mesh_id,p.state,p.last_seen_at FROM fed_peer p "
            "LEFT JOIN fed_topology_policy t ON t.peer_id=p.id "
            "WHERE p.state='active' AND (t.last_sent_at IS NULL OR t.last_sent_at<=?) "
            "ORDER BY p.mesh_id",
            (stamp - TOPOLOGY_INTERVAL_SECONDS,),
        )
        return [
            str(row["mesh_id"])
            for row in rows
            if self.peers.is_online_at(str(row["state"]), row["last_seen_at"], now=stamp)
        ]

    async def mark_sent(self, mesh_id: str, *, now: int | None = None) -> None:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        peer = await self.peers.by_mesh_id(mesh_id)
        await self.database.write(
            "INSERT INTO fed_topology_policy(peer_id,share_location,precision_km,updated_at,"
            "updated_by,last_sent_at) VALUES(?,0,10,?,'system',?) "
            "ON CONFLICT(peer_id) DO UPDATE SET last_sent_at=excluded.last_sent_at",
            (peer.id, stamp, stamp),
        )

    async def accept(
        self, sender_mesh_id: str, value: dict[str, Any], *, now: int | None = None
    ) -> None:
        stamp = int(self.clock.now().timestamp()) if now is None else now
        peer = await self.peers.by_mesh_id(sender_mesh_id)
        if peer.state != "active":
            raise ValueError("topology update requires an active paired peer")
        if set(value) != {"generated_at", "location"}:
            raise ValueError("topology update has missing or unknown fields")
        raw_generated_at = value["generated_at"]
        if isinstance(raw_generated_at, bool) or not isinstance(raw_generated_at, int):
            raise ValueError("invalid topology update timestamp")
        generated_at = raw_generated_at
        if generated_at > stamp + TOPOLOGY_CLOCK_SKEW_SECONDS:
            raise ValueError("topology update timestamp is in the future")
        location = value["location"]
        shared = location is not None
        latitude = longitude = precision = None
        if shared:
            if not isinstance(location, dict) or set(location) != {
                "lat",
                "lon",
                "precision_km",
            }:
                raise ValueError("invalid topology location")
            raw_values = (location["lat"], location["lon"], location["precision_km"])
            if any(
                isinstance(raw, bool) or not isinstance(raw, (int, float)) for raw in raw_values
            ):
                raise ValueError("invalid topology location values")
            latitude, longitude, precision = (float(raw) for raw in raw_values)
            if not all(math.isfinite(number) for number in (latitude, longitude, precision)):
                raise ValueError("invalid topology location values")
            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError("topology location is outside coordinate bounds")
            if not 1 <= precision <= 100:
                raise ValueError("topology precision must be 1-100 km")
        existing = await self.database.read(
            "SELECT generated_at FROM fed_topology_peer WHERE peer_id=?", (peer.id,)
        )
        if existing and generated_at < int(existing[0]["generated_at"]):
            raise ValueError("stale topology update")
        await self.database.write(
            "INSERT INTO fed_topology_peer(peer_id,location_shared,location_lat,location_lon,"
            "precision_km,generated_at,received_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(peer_id) DO UPDATE SET location_shared=excluded.location_shared,"
            "location_lat=excluded.location_lat,location_lon=excluded.location_lon,"
            "precision_km=excluded.precision_km,generated_at=excluded.generated_at,"
            "received_at=excluded.received_at",
            (
                peer.id,
                int(shared),
                latitude,
                longitude,
                precision,
                generated_at,
                stamp,
            ),
        )

    async def overview(self) -> dict[str, Any]:
        now = int(self.clock.now().timestamp())
        peer_rows = await self.database.read(
            "SELECT p.id,p.mesh_id,p.node_name,p.state,p.protocol_version,p.capabilities,"
            "p.discovery_transports,p.last_seen_at,p.last_sync_at,p.boards,p.sync_incidents,"
            "p.relay_alerts,p.relay_mail,p.service_permissions,p.policy_configured,"
            "p.policy_applied_by,p.policy_applied_at,p.policy_review_at,"
            "r.enabled relay_enabled,r.paused relay_paused,r.scopes_json relay_scopes,"
            "t.location_shared,t.location_lat remote_lat,t.location_lon remote_lon,"
            "t.precision_km remote_precision,t.received_at location_received_at,"
            "l.share_location,l.location_lat policy_lat,l.location_lon policy_lon,"
            "l.precision_km policy_precision,l.updated_at location_policy_updated_at "
            "FROM fed_peer p LEFT JOIN fed_relay_policy r ON r.peer_id=p.id "
            "LEFT JOIN fed_topology_peer t ON t.peer_id=p.id "
            "LEFT JOIN fed_topology_policy l ON l.peer_id=p.id "
            "ORDER BY p.last_seen_at DESC,p.mesh_id"
        )
        successors = await self.database.read(
            "SELECT s.old_mesh_id,s.old_node_name,s.adopted_at,s.adopted_by,"
            "p.mesh_id successor_mesh_id,p.node_name successor_name "
            "FROM fed_peer_successor s JOIN fed_peer p ON p.id=s.successor_peer_id"
        )
        successor_by_peer: dict[str, list[dict[str, Any]]] = {}
        for row in successors:
            successor_by_peer.setdefault(str(row["successor_mesh_id"]), []).append(dict(row))
        items = [
            await self._peer_overview(row, successor_by_peer.get(str(row["mesh_id"]), []), now)
            for row in peer_rows
        ]
        current_ids = {str(row["mesh_id"]) for row in peer_rows}
        for row in successors:
            if str(row["old_mesh_id"]) in current_ids:
                continue
            audit = await self.database.read(
                "SELECT actor_kind,actor_ref,action,created_at FROM audit_log "
                "WHERE action='federation.origin_adopt' AND detail=? "
                "ORDER BY created_at DESC,id DESC LIMIT 10",
                (row["old_mesh_id"],),
            )
            items.append(
                {
                    "mesh_id": row["old_mesh_id"],
                    "node_name": row["old_node_name"],
                    "state": "adopted",
                    "raw_state": "former",
                    "identity_kind": "adopted",
                    "successor_mesh_id": row["successor_mesh_id"],
                    "successor_name": row["successor_name"],
                    "adopted_at": row["adopted_at"],
                    "location": None,
                    "transports": [],
                    "degraded": False,
                    "degraded_reasons": [],
                    "backlog": 0,
                    "audit": [dict(event) for event in audit],
                }
            )
        tombstones = await self.database.read(
            "SELECT mesh_id,node_name,forgotten_at,forgotten_by FROM fed_peer_tombstone "
            "ORDER BY forgotten_at DESC"
        )
        represented = {str(item["mesh_id"]) for item in items}
        for row in tombstones:
            if str(row["mesh_id"]) in represented:
                continue
            audit = await self.database.read(
                "SELECT actor_kind,actor_ref,action,created_at FROM audit_log "
                "WHERE target IN (?,?) ORDER BY created_at DESC,id DESC LIMIT 10",
                (row["mesh_id"], f"fed_peer:{row['mesh_id']}"),
            )
            items.append(
                {
                    **dict(row),
                    "state": "forgotten",
                    "raw_state": "forgotten",
                    "identity_kind": "forgotten",
                    "location": None,
                    "transports": [],
                    "degraded": False,
                    "degraded_reasons": [],
                    "backlog": 0,
                    "audit": [dict(event) for event in audit],
                }
            )
        counts: dict[str, int] = {}
        for item in items:
            state = str(item["state"])
            counts[state] = counts.get(state, 0) + 1
        return {"items": items, "counts": counts, "generated_at": now}

    async def _peer_overview(
        self, row: Any, predecessors: list[dict[str, Any]], now: int
    ) -> dict[str, Any]:
        peer_id, mesh_id = int(row["id"]), str(row["mesh_id"])
        transports = json.loads(str(row["discovery_transports"]))
        paths = await self.database.read(
            "SELECT COALESCE(transport,'unknown') transport,MAX(created_at) last_at,"
            "COUNT(*) count_24h FROM message_log WHERE airtime_class='federation' "
            "AND direction='in' AND peer_mesh_id=? AND COALESCE(outcome,'')<>'rejected' "
            "AND created_at>=? GROUP BY transport",
            (mesh_id, now - 86_400),
        )
        path_map = {str(path["transport"]): dict(path) for path in paths}
        successful = max(paths, key=lambda path: int(path["last_at"] or 0), default=None)
        last_successful_path = str(successful["transport"]) if successful else None
        preferred_path = last_successful_path or (
            "radio" if "radio" in transports else "mqtt" if "mqtt" in transports else None
        )
        metrics = (
            await self.database.read(
                "SELECT "
                "(SELECT COUNT(*) FROM fed_inbox_item WHERE peer_id=? AND state='pending')+"
                "(SELECT COUNT(*) FROM fed_post_delivery WHERE peer_id=? AND state<>'delivered')+"
                "(SELECT COUNT(*) FROM fed_mail_delivery WHERE peer_id=? "
                " AND state IN ('queued','sent'))+"
                "(SELECT COUNT(*) FROM fed_service_request WHERE peer_mesh_id=? "
                " AND status='pending')+"
                "(SELECT COUNT(*) FROM fed_relay_envelope WHERE "
                " (received_from_peer_id=? OR next_hop_mesh_id=?) AND state IN "
                " ('queued','quarantined','paused','forwarding','forwarded')) backlog,"
                "(SELECT COUNT(*) FROM fed_post_delivery WHERE peer_id=? AND error IS NOT NULL)+"
                "(SELECT COUNT(*) FROM fed_mail_delivery WHERE peer_id=? AND error IS NOT NULL) "
                "delivery_errors,"
                "(SELECT COUNT(*) FROM message_log WHERE peer_mesh_id=? AND direction='in' "
                " AND airtime_class='federation' AND outcome='rejected' "
                " AND created_at>=?) rejected",
                (
                    peer_id,
                    peer_id,
                    peer_id,
                    mesh_id,
                    peer_id,
                    mesh_id,
                    peer_id,
                    peer_id,
                    mesh_id,
                    now - 86_400,
                ),
            )
        )[0]
        raw_state = str(row["state"])
        offline = raw_state == "active" and not self.peers.is_online_at(
            raw_state, row["last_seen_at"], now=now
        )
        state = "discovered" if raw_state == "pending" else "offline" if offline else raw_state
        sync_enabled = bool(json.loads(str(row["boards"]))) or bool(row["sync_incidents"])
        degraded_reasons: list[str] = []
        if offline:
            stale_hours = self.peers.peer_stale_seconds // 3_600
            degraded_reasons.append(
                f"Peer has not been seen for {stale_hours} hours; federation traffic is paused"
            )
        if sync_enabled and (
            row["last_sync_at"] is None or now - int(row["last_sync_at"]) > 86_400
        ):
            degraded_reasons.append("Configured sync has not succeeded for 24 hours")
        if int(metrics["delivery_errors"]):
            degraded_reasons.append("Delivery errors need review")
        if int(metrics["rejected"]):
            degraded_reasons.append("Authenticated traffic was rejected in the last 24 hours")
        location = None
        if raw_state == "active" and bool(row["location_shared"] or 0):
            location = {
                "lat": float(row["remote_lat"]),
                "lon": float(row["remote_lon"]),
                "precision_km": float(row["remote_precision"]),
                "received_at": int(row["location_received_at"]),
            }
        audit = await self.database.read(
            "SELECT actor_kind,actor_ref,action,created_at FROM audit_log "
            "WHERE target IN (?,?) ORDER BY created_at DESC,id DESC LIMIT 10",
            (mesh_id, f"fed_peer:{peer_id}"),
        )
        return {
            "mesh_id": mesh_id,
            "node_name": row["node_name"],
            "state": state,
            "raw_state": raw_state,
            "identity_kind": "successor" if predecessors else "current",
            "predecessors": predecessors,
            "protocol_version": row["protocol_version"],
            "capabilities": json.loads(str(row["capabilities"])),
            "transports": transports,
            "paths": {
                name: path_map.get(name, {"transport": name, "last_at": None, "count_24h": 0})
                for name in ("radio", "mqtt")
            },
            "preferred_path": preferred_path,
            "last_successful_path": last_successful_path,
            "last_seen_at": row["last_seen_at"],
            "last_sync_at": row["last_sync_at"],
            "backlog": int(metrics["backlog"]),
            "degraded": bool(degraded_reasons),
            "degraded_reasons": degraded_reasons,
            "location": location,
            "location_policy": (
                {
                    "share_location": bool(row["share_location"] or 0),
                    "lat": row["policy_lat"],
                    "lon": row["policy_lon"],
                    "precision_km": float(row["policy_precision"] or 10),
                    "updated_at": row["location_policy_updated_at"],
                }
                if raw_state in {"active", "paused"}
                else None
            ),
            "delivery": {
                "backlog": int(metrics["backlog"]),
                "errors": int(metrics["delivery_errors"]),
                "rejected_24h": int(metrics["rejected"]),
            },
            "services": json.loads(str(row["service_permissions"])),
            "policy": {
                "configured": bool(row["policy_configured"]),
                "boards": json.loads(str(row["boards"])),
                "sync_incidents": bool(row["sync_incidents"]),
                "relay_alerts": bool(row["relay_alerts"]),
                "relay_mail": bool(row["relay_mail"]),
                "relay_enabled": bool(row["relay_enabled"] or 0),
                "relay_paused": bool(row["relay_paused"] or 0),
                "relay_scopes": json.loads(str(row["relay_scopes"] or "[]")),
                "applied_by": row["policy_applied_by"],
                "applied_at": row["policy_applied_at"],
                "review_at": row["policy_review_at"],
            },
            "audit": [dict(event) for event in audit],
        }
