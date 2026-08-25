from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import defaultdict

from outpost import __version__
from outpost.commands.core import specs as core_specs
from outpost.config import Config
from outpost.render.catalogue import message
from outpost.security.rate_limit import RateLimiter
from outpost.store.members import MemberRepo
from outpost.transport.models import InboundMessage

from .models import CommandContext, Line, Response, ResponseKind, TrustLevel
from .registry import CommandRegistry
from .session import SessionStore


class Router:
    def __init__(
        self, config: Config, members: MemberRepo, sessions: SessionStore, rate_limiter: RateLimiter
    ) -> None:
        self.config, self.members, self.sessions, self.rate_limiter = (
            config,
            members,
            sessions,
            rate_limiter,
        )
        self.registry = CommandRegistry()
        for spec in core_specs():
            self.registry.register(spec)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _invoked(self, inbound: InboundMessage) -> str | None:
        if inbound.text is None:
            return None
        text = unicodedata.normalize("NFKC", inbound.text).strip()
        if inbound.is_direct:
            prefix = self.config.router.prefix
            return text[len(prefix) :].lstrip() if text.startswith(prefix) else text
        prefix = re.escape(self.config.router.prefix)
        match = re.match(rf"^{prefix}\s*(.*)$", text, re.IGNORECASE)
        if match:
            return match.group(1)
        short = re.escape(self.config.node.short_name)
        match = re.match(rf"^{short}\s+(.+)$", text, re.IGNORECASE)
        return match.group(1) if match else None

    async def dispatch(self, inbound: InboundMessage) -> Response:
        invoked = self._invoked(inbound)
        if invoked is None or inbound.no_reply:
            return Response(ResponseKind.NONE)
        member = await self.members.resolve(inbound.from_id)
        if member.trust == "blocked":
            return Response(ResponseKind.NONE)
        channel = -1 if inbound.is_direct else inbound.channel
        session = self.sessions.get(member.mesh_id, channel)
        parts = invoked.split(maxsplit=1)
        token = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        if not await self.rate_limiter.allow(member.mesh_id, member.trust, token):
            return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
        spec = self.registry.resolve(token)
        if spec is None:
            return Response(ResponseKind.ERROR, [Line(message("unknown"))])
        if TrustLevel.parse(member.trust) < spec.min_trust:
            return Response(ResponseKind.ERROR, [Line(message("unknown"))])
        ctx = CommandContext(
            message=inbound,
            member=member,
            session=session,
            args=args,
            registry=self.registry,
            node_name=self.config.node.name,
            operator_contact=self.config.node.operator_contact,
            version=__version__,
            disclaimer=self.config.node.disclaimer,
            attribution=(
                " · Weather fallback data: Open-Meteo" if self.config.modules.env.enabled else ""
            ),
        )
        lock = self._locks[member.mesh_id]
        try:
            async with asyncio.timeout(self.config.router.member_lock_timeout_s):
                async with lock:
                    return await spec.handler(ctx)
        except TimeoutError:
            return Response(ResponseKind.ERROR, [Line(message("internal_error"))])
        except Exception:
            return Response(ResponseKind.ERROR, [Line(message("internal_error"))])
