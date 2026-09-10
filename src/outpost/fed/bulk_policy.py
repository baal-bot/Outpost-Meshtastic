"""Transaction-owned IP replay/work primitives; no listener or domain importer."""

from __future__ import annotations

import sqlite3
from typing import Any

from outpost.config import Config, FedBulkConfig
from outpost.fed import bulk_format as fmt
from outpost.fed.peers import FederationPeerService
from outpost.store import Database, Transaction

MAX_PEERS = 32
MAX_RETAINED_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 5


class BulkDenied(ValueError):
    pass


class BulkReplay(BulkDenied):
    pass


class BulkStorageFull(BulkDenied):
    pass


class BulkLedger:
    """The caller must include domain effects in the same owned transaction.

    B2 will supply the shared policy/import operation. These primitives alone
    cannot attest to domain storage, human review, TLS health or RF delivery.
    """

    def __init__(self, database: Database, peers: FederationPeerService, config: Config) -> None:
        self.database, self.peers, self.config = database, peers, config

    def _configuration_digest(self, peer: str) -> str:
        # Bind prepared work to the exact operator-selected endpoint/TLS policy.
        # A future audited rebind may change this digest while preserving counters.
        try:
            config = FedBulkConfig.model_validate(self.config.fed.bulk.model_dump())
        except ValueError as error:
            raise BulkDenied("bulk endpoint configuration is invalid") from error
        endpoint = next((endpoint for endpoint in config.peers if endpoint.mesh_id == peer), None)
        if endpoint is None:
            raise BulkDenied("bulk peer endpoint is not explicitly configured")
        return fmt.digest(
            fmt.canonical(
                [
                    config.listen_address,
                    config.listen_port,
                    str(config.certificate_file),
                    str(config.private_key_file),
                    str(config.trust_root_file),
                    config.test_only_loopback,
                    endpoint.mesh_id,
                    endpoint.address,
                    endpoint.port,
                    endpoint.certificate_identity,
                    endpoint.public_key_sha256,
                ]
            )
        )

    async def _authority(self, tx: Transaction, peer: str) -> tuple[int, str, bytes, str]:
        tx.check_owner(self.database)
        if not self.config.modules.fed.enabled or not self.config.fed.bulk.enabled:
            raise BulkDenied("bulk is disabled")
        binding = self._configuration_digest(peer)
        if await tx.read("SELECT 1 FROM recovery_fence WHERE id=1"):
            raise BulkDenied("restored identity is fenced")
        identity = await tx.read("SELECT mesh_id FROM fed_bundle_identity WHERE id=1")
        if not identity or identity[0]["mesh_id"] != self.peers.local_mesh_id:
            raise BulkDenied("bulk requires the current commissioned local identity")
        local = fmt.node(self.peers.local_mesh_id)
        fmt.node(peer)
        rows = await tx.read(
            "SELECT id,shared_secret FROM fed_peer WHERE mesh_id=? AND state='active' "
            "AND local_approved=1 AND remote_approved=1 AND shared_secret IS NOT NULL",
            (peer,),
        )
        if not rows:
            raise BulkDenied("bulk requires current mutually approved pairing")
        secret = bytes(rows[0]["shared_secret"])
        fmt.generation(secret, local, peer)
        return int(rows[0]["id"]), local, secret, binding

    @staticmethod
    async def _write(tx: Transaction, sql: str, params: tuple[Any, ...]) -> None:
        try:
            await tx.write(sql, params)
        except sqlite3.IntegrityError as error:
            if "bulk storage full" in str(error):
                raise BulkStorageFull("bulk storage full; replay state was preserved") from error
            raise

    async def _state(
        self, tx: Transaction, peer_id: int, local: str, generation: str, binding: str
    ) -> sqlite3.Row:
        rows = await tx.read("SELECT * FROM fed_bulk_state WHERE peer_id=?", (peer_id,))
        if not rows:
            await self._write(
                tx,
                "INSERT INTO fed_bulk_state(peer_id,local_mesh_id,generation,configuration_digest) "
                "VALUES(?,?,?,?)",
                (peer_id, local, generation, binding),
            )
            rows = await tx.read("SELECT * FROM fed_bulk_state WHERE peer_id=?", (peer_id,))
        row = rows[0]
        if row["generation"] != generation or row["local_mesh_id"] != local:
            raise BulkDenied("bulk state belongs to a different identity or pairing generation")
        if row["configuration_digest"] != binding:
            raise BulkDenied("bulk endpoint or TLS policy changed; an explicit rebind is required")
        return row

    async def prepare(
        self, tx: Transaction, peer: str, operation: str, payload: dict[str, Any]
    ) -> bytes:
        """Persist exact request bytes before any I/O; retry does not allocate a sequence."""
        peer_id, local, secret, binding = await self._authority(tx, peer)
        row = await self._state(tx, peer_id, local, fmt.generation(secret, local, peer), binding)
        if row["tx_request"] is not None:
            raw = bytes(row["tx_request"])
            pending = fmt.decode_request(raw, secret, local, peer)
            if pending.operation != operation or pending.payload_bytes != fmt.canonical(payload):
                raise BulkDenied("another exact bulk request is pending")
            return raw
        sequence = int(row["tx_sequence"]) + 1
        raw = fmt.encode_request(secret, local, peer, sequence, operation, payload)
        await self._write(
            tx,
            "UPDATE fed_bulk_state SET tx_sequence=?,tx_request=?,tx_attempts=0 WHERE peer_id=?",
            (sequence, raw, peer_id),
        )
        return raw

    async def reserve_attempt(self, tx: Transaction, peer: str) -> bytes:
        """Reserve before I/O. Restarts never replenish the five-attempt allowance."""
        peer_id, local, secret, binding = await self._authority(tx, peer)
        row = await self._state(tx, peer_id, local, fmt.generation(secret, local, peer), binding)
        if row["tx_request"] is None:
            raise BulkDenied("no pending bulk request")
        if int(row["tx_attempts"]) >= MAX_ATTEMPTS:
            raise BulkDenied("bulk retries paused after five attempts")
        raw = bytes(row["tx_request"])
        fmt.decode_request(raw, secret, local, peer)
        await tx.write(
            "UPDATE fed_bulk_state SET tx_attempts=tx_attempts+1 WHERE peer_id=?", (peer_id,)
        )
        return raw

    async def inspect_request(
        self, tx: Transaction, peer: str, raw: bytes
    ) -> tuple[fmt.BulkMessage, bytes | None]:
        """Authenticate/recheck replay; only record_response advances receive state."""
        peer_id, local, secret, binding = await self._authority(tx, peer)
        message = fmt.decode_request(raw, secret, peer, local)
        row = await self._state(tx, peer_id, local, message.generation, binding)
        previous = int(row["rx_sequence"])
        if message.sequence == previous and fmt.digest(raw) == row["rx_request_digest"]:
            return message, bytes(row["rx_response"])
        if message.sequence != previous + 1:
            raise BulkReplay("bulk replay, changed request, or noncontiguous sequence")
        return message, None

    async def record_response(
        self, tx: Transaction, peer: str, raw_request: bytes, raw_response: bytes
    ) -> bytes:
        """Commit only alongside the operation/domain result, never at HTTP admission."""
        request, cached = await self.inspect_request(tx, peer, raw_request)
        peer_id, local, secret, binding = await self._authority(tx, peer)
        await self._state(tx, peer_id, local, request.generation, binding)
        response = fmt.decode_response(raw_response, secret, request)
        if response.payload["status"] in fmt.TRANSIENT_STATUSES:
            raise BulkDenied("transient bulk responses must not commit receive state")
        if cached is not None:
            if cached != raw_response:
                raise BulkReplay("an exact request already has a different committed response")
            return cached
        await self._write(
            tx,
            "UPDATE fed_bulk_state SET rx_sequence=?,rx_request_digest=?,rx_response=? "
            "WHERE peer_id=?",
            (request.sequence, fmt.digest(raw_request), raw_response, peer_id),
        )
        return raw_response

    async def accept_response(self, tx: Transaction, peer: str, raw: bytes) -> fmt.BulkMessage:
        """Clear pending work only in the transaction that applies its verified result."""
        peer_id, local, secret, binding = await self._authority(tx, peer)
        row = await self._state(tx, peer_id, local, fmt.generation(secret, local, peer), binding)
        if row["tx_request"] is None:
            raise BulkReplay("response has no pending bulk request")
        request = fmt.decode_request(bytes(row["tx_request"]), secret, local, peer)
        response = fmt.decode_response(raw, secret, request)
        if response.payload["status"] in fmt.TRANSIENT_STATUSES:
            # A busy peer must not replenish attempts by turning one operation
            # into an unbounded succession of supposedly new requests.
            return response
        await tx.write(
            "UPDATE fed_bulk_state SET tx_request=NULL,tx_attempts=0 WHERE peer_id=?", (peer_id,)
        )
        return response
