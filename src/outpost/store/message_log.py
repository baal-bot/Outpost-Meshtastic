from __future__ import annotations

from dataclasses import dataclass

from outpost.clock import Clock
from outpost.transport.models import InboundMessage

from .database import Database


@dataclass(frozen=True)
class MessageLogEntry:
    id: int
    direction: str
    peer_mesh_id: str | None
    text: str | None
    outcome: str | None
    created_at: int


class MessageLogRepo:
    def __init__(self, database: Database, clock: Clock) -> None:
        self.database, self.clock = database, clock

    async def record_inbound(self, message: InboundMessage) -> int:
        text_bytes = message.text.encode() if message.text is not None else b""
        payload_bytes = message.payload or b""
        return await self.database.write(
            """
            INSERT INTO message_log(
              direction,peer_mesh_id,channel,portnum,is_direct,packet_id,text,byte_len,
              outcome,rx_snr,rx_rssi,hops,transport,created_at,to_mesh_id,payload,
              want_ack,pki_encrypted,pki_public_key,no_reply,request_id,routing_error,
              latitude,longitude,rx_time
            ) VALUES('in',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                message.from_id,
                message.channel,
                message.portnum,
                int(message.is_direct),
                message.packet_id,
                message.text,
                len(text_bytes or payload_bytes),
                "received",
                message.rx_snr,
                message.rx_rssi,
                message.hops_away,
                "mqtt" if message.via_mqtt else "radio",
                int(self.clock.now().timestamp()),
                message.to_id,
                message.payload,
                int(message.want_ack),
                int(message.pki_encrypted),
                message.pki_public_key,
                int(message.no_reply),
                message.request_id,
                message.routing_error,
                message.latitude,
                message.longitude,
                int(message.rx_time.timestamp()),
            ),
        )

    async def record_outbound(
        self,
        *,
        peer_mesh_id: str,
        channel: int,
        portnum: int,
        packet_id: int | None,
        text: str | None,
        byte_len: int,
        toa_ms: int,
        airtime_class: str,
        outcome: str,
        is_direct: bool,
        command: str | None = None,
        drop_reason: str | None = None,
        in_reply_to_id: int | None = None,
    ) -> int:
        return await self.database.write(
            """
            INSERT INTO message_log(
              direction,peer_mesh_id,channel,portnum,is_direct,packet_id,text,byte_len,
              toa_ms,airtime_class,command,outcome,drop_reason,transport,in_reply_to_id,created_at
            ) VALUES('out',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                peer_mesh_id,
                channel,
                portnum,
                int(is_direct),
                packet_id,
                text,
                byte_len,
                toa_ms,
                airtime_class,
                command,
                outcome,
                drop_reason,
                "mesh",
                in_reply_to_id,
                int(self.clock.now().timestamp()),
            ),
        )

    async def recent(self, limit: int = 50) -> list[MessageLogEntry]:
        rows = await self.database.read(
            """
            SELECT id,direction,peer_mesh_id,text,outcome,created_at
            FROM message_log ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return [MessageLogEntry(**dict(row)) for row in rows]

    async def resolve_ack(self, packet_id: int, outcome: str) -> bool:
        async with self.database.transaction() as transaction:
            rows = await transaction.read(
                "SELECT m.id,m.outbox_id FROM message_log m "
                "LEFT JOIN outbound_work w ON w.id=m.outbox_id "
                "WHERE m.direction='out' AND m.packet_id=? "
                "ORDER BY CASE WHEN w.state='awaiting_ack' THEN 0 ELSE 1 END,m.id DESC LIMIT 1",
                (packet_id,),
            )
            if not rows:
                return False
            await transaction.write(
                "UPDATE message_log SET outcome=? WHERE id=?",
                (outcome, rows[0]["id"]),
            )
            if rows[0]["outbox_id"] is not None:
                await transaction.write(
                    "UPDATE outbound_work SET state=?,outcome=?,completed_at=unixepoch() "
                    "WHERE id=? AND state='awaiting_ack'",
                    (
                        "acked" if outcome == "acked" else "failed",
                        outcome,
                        rows[0]["outbox_id"],
                    ),
                )
        return True

    async def mark_inbound_dropped(self, log_id: int, reason: str) -> None:
        await self.database.write(
            "UPDATE message_log SET outcome='dropped',drop_reason=? WHERE id=? AND direction='in'",
            (reason, log_id),
        )
