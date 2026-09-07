"""Temporary Outposts on a lossless, serialized single-hop simulated radio medium.

Application framing, authentication, pacing and durable queueing are real. The
medium schedules arrival after modeled packet airtime; it is not a Meshtastic
firmware, collision, propagation-range or physical-radio simulator.
"""

from __future__ import annotations

import heapq
import io
import json
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from PIL import Image, ImageDraw

from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.transport.models import InboundMessage
from outpost.transport.simulated import SimulatedRadioLink
from outpost.transport.toa import toa
from tests.integration.test_incident_location import verified_member
from tests.integration.test_safety_commands import inbound

REPORTER = "!0000feed"
REPORTER_KEY = bytes(range(32))
REPORT_TEXT = "REPORT fallen tree 40.4406 -79.9959"


def synthetic_tiles(root):
    """A local rendering fixture, explicitly not real geographic coverage."""
    tile = Image.new("RGB", (256, 256), "#dce8d8")
    draw = ImageDraw.Draw(tile)
    draw.rectangle((0, 0, 255, 255), outline="#74856c", width=2)
    draw.line((0, 128, 256, 128), fill="#a6b49e", width=2)
    draw.text((10, 16), "SYNTHETIC G6 TEST TILE", fill="#243820")
    output = io.BytesIO()
    tile.save(output, format="PNG")
    data = output.getvalue()
    count = 0
    for zoom in range(11, 20):
        x = int((-79.9959 + 180) / 360 * 2**zoom)
        y = int((1 - math.asinh(math.tan(math.radians(40.4406))) / math.pi) / 2 * 2**zoom)
        for dx in range(-3, 4):
            directory = root / str(zoom) / str(x + dx)
            directory.mkdir(parents=True, exist_ok=True)
            for dy in range(-3, 4):
                (directory / f"{y + dy}.png").write_bytes(data)
                count += 1
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "source": "Synthetic G6 rendering fixture, not geographic map data",
                "tile_extension": "png",
                "tile_count": count,
            }
        ),
        encoding="utf-8",
    )


class IncidentMesh:
    def __init__(self, clock, apps):
        self.clock, self.apps = clock, apps
        self.offsets = {app.radio.local_node_id: 0 for app in apps}
        self.medium_free_at = 0.0
        self.arrivals = []
        self.sequence = 0
        self.transmissions = []
        # The simulation clock begins at intake arrival. End-to-end measurements
        # add the preceding handheld packet's modeled first-hop airtime.
        self.field_airtime = toa(len(REPORT_TEXT.encode()), "LONG_FAST", portnum=1)

    async def receive(self, app, message):
        app.inbound_pipeline.local_node_id = app.radio.local_node_id
        checked = app.inbound_pipeline.process(message)
        if checked is not None:
            await app.message_log.record_inbound(checked)
            await app._handle_inbound_message(checked)

    async def report(self):
        source = self.apps[0]
        await verified_member(source, REPORTER, REPORTER_KEY)
        await self.receive(
            source,
            inbound(9001, REPORT_TEXT, REPORTER, key=REPORTER_KEY),
        )
        return (await source.incidents.list())[0]

    async def tick(self):
        now = self.clock.monotonic()
        # Include a full initial worker polling phase, not lucky t=0 admission.
        if now >= 5 and int(now) % 5 == 0:
            for app in self.apps:
                await app._incident_delivery_once()
        for app in self.apps:
            await app.governor.tick()
            sender = app.radio.local_node_id
            while self.offsets[sender] < len(app.radio.sent):
                packet_id = self.offsets[sender] + 1
                packet = app.radio.sent[self.offsets[sender]]
                self.offsets[sender] += 1
                size = len(packet.payload or (packet.text or "").encode())
                port = app.config.radio.federation_portnum if packet.payload else 1
                duration = toa(size, "LONG_FAST", portnum=port)
                start = max(now, self.medium_free_at)
                self.medium_free_at = start + duration
                self.sequence += 1
                heapq.heappush(
                    self.arrivals,
                    (self.medium_free_at, self.sequence, app, packet_id, packet, port),
                )
                self.transmissions.append((sender, packet_id, start, self.medium_free_at, size))
        while self.arrivals and self.arrivals[0][0] <= now:
            _, _, source, packet_id, packet, port = heapq.heappop(self.arrivals)
            if packet.payload:
                for target in self.apps:
                    if target is source or packet.dest not in ("^all", target.radio.local_node_id):
                        continue
                    await self.receive(
                        target,
                        InboundMessage(
                            packet_id,
                            source.radio.local_node_id,
                            packet.dest,
                            packet.channel,
                            port,
                            False,
                            None,
                            packet.payload,
                            self.clock.now(),
                        ),
                    )
            elif packet.dest == REPORTER and packet.want_ack:
                # The connected synthetic reporter receives the reply. Firmware
                # ACK signaling is represented at the routing-event boundary.
                await source._handle_inbound_message(
                    InboundMessage(
                        10000 + packet_id,
                        REPORTER,
                        source.radio.local_node_id,
                        packet.channel,
                        5,
                        True,
                        None,
                        None,
                        self.clock.now(),
                        request_id=packet_id,
                        routing_error="NONE",
                    )
                )


@asynccontextmanager
async def incident_mesh(root, count=3):
    clock = VirtualClock(epoch=datetime.now(UTC) - timedelta(hours=1))
    apps = []
    try:
        for index in range(count):
            node_id = f"!00000a0{index}"
            config = Config.model_validate(
                {
                    "store": {
                        "path": str(root / f"node-{index}.db"),
                        "tiles_path": str(root / "tiles"),
                        "releases_path": str(root / "absent-releases"),
                    },
                    "modules": {"fed": {"enabled": True}, "watch": {"enabled": True}},
                    "airtime": {"quiet_hours": {"classes": []}},
                }
            )
            app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id=node_id))
            await app.database.open()
            apps.append(app)
            await app.radio.connect()
            app.federation.local_mesh_id = node_id
            app.federation_sync.local_mesh_id = node_id
            app.incidents.origin_node = node_id
        for index, app in enumerate(apps):
            for other, target in enumerate(apps):
                if app is target:
                    continue
                peer = await app.federation.discover(
                    target.radio.local_node_id,
                    f"Synthetic Outpost {other}",
                    1,
                    {
                        "reconciliation": 2,
                        "incident_events": 1,
                        "incident_updates": 1,
                        "incident_compact": 1,
                    },
                    "radio",
                )
                pair = sorted((index, other))
                secret = bytes([1 + pair[0] * count + pair[1]]) * 32
                await app.database.write(
                    "UPDATE fed_peer SET state='active',shared_secret=?,sync_incidents=1,"
                    "local_approved=1,remote_approved=1,policy_configured=1,"
                    "policy_applied_by='test:g6',incident_lat=40.4406,"
                    "incident_lon=-79.9959,incident_radius_km=25,last_sync_at=? WHERE id=?",
                    (secret, int(clock.now().timestamp()), peer.id),
                )
        yield IncidentMesh(clock, apps)
    finally:
        for app in reversed(apps):
            await app.ai_service.close()
            await app.radio.close()
            await app.database.close()
