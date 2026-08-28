from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import defaultdict

from outpost import __version__
from outpost.commands.core import specs as core_specs
from outpost.config import Config
from outpost.render.catalogue import message
from outpost.security.rate_limit import SAFETY_FLOOR, RateLimiter
from outpost.store.members import MemberRepo
from outpost.transport.models import InboundMessage

from .intents import IntentResolver
from .models import CommandContext, Line, Response, ResponseKind, TrustLevel
from .registry import CommandRegistry
from .session import SessionStore
from .tui import TuiController


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
        self.tui = TuiController()
        self.intents = IntentResolver(config.router.intents_file)

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

    def command_token(self, inbound: InboundMessage) -> str | None:
        invoked = self._invoked(inbound)
        if invoked is None:
            return None
        parts = invoked.split(maxsplit=1)
        return parts[0].upper() if parts else ""

    async def dispatch(self, inbound: InboundMessage, *, ordered: bool = True) -> Response:
        try:
            async with asyncio.timeout(self.config.router.member_lock_timeout_s):
                if not ordered:
                    return await self._dispatch_unlocked(inbound)
                lock = self._locks[inbound.from_id]
                async with lock:
                    return await self._dispatch_unlocked(inbound)
        except TimeoutError:
            return Response(ResponseKind.ERROR, [Line(message("internal_error"))])

    async def _dispatch_unlocked(self, inbound: InboundMessage) -> Response:
        invoked = self._invoked(inbound)
        if invoked is None or inbound.no_reply:
            return Response(ResponseKind.NONE)
        member = await self.members.resolve(
            inbound.from_id,
            last_heard_snr=inbound.rx_snr,
            hops_away=inbound.hops_away,
        )
        if member.trust == "blocked":
            return Response(ResponseKind.NONE)
        channel = -1 if inbound.is_direct else inbound.channel
        session = self.sessions.get(member.mesh_id, channel)
        parts = invoked.split(maxsplit=1)
        token = parts[0] if parts else ""
        if self.registry.resolve(token) is not None:
            self.tui.cancel_for_command(session)
        else:
            invoked, pending_response = self.tui.prepare(
                invoked,
                session,
                self.sessions.clock.monotonic(),
                direct=inbound.is_direct,
            )
            if pending_response is not None:
                if not await self.rate_limiter.allow(member.mesh_id, member.trust, "MENU"):
                    return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
                return pending_response
            parts = invoked.split(maxsplit=1)
            token = parts[0] if parts else ""
        if self.registry.resolve(token) is None:
            invoked, _match_mode = self.intents.resolve(
                invoked,
                member.trust,
                self.registry,
            )
            parts = invoked.split(maxsplit=1)
            token = parts[0] if parts else ""
        if self.registry.resolve(token) is not None:
            self.tui.cancel_for_command(session)
        args = parts[1] if len(parts) > 1 else ""
        if not await self.rate_limiter.allow(member.mesh_id, member.trust, token):
            return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
        spec = self.registry.resolve(token)
        if spec is None and inbound.is_direct and self.config.modules.ai.enabled:
            if session.tui_active:
                return Response(ResponseKind.ERROR, [Line("Not an option. Send ? for the menu.")])
            spec = self.registry.resolve("ASK")
            args = invoked
        if spec is None:
            return Response(ResponseKind.ERROR, [Line(message("unknown"))])
        if TrustLevel.parse(member.trust) < spec.min_trust:
            return Response(ResponseKind.ERROR, [Line(message("unknown"))])
        safety_decision = None
        if token.upper() in SAFETY_FLOOR:
            decision = await self.rate_limiter.safety_floor_decision(member.mesh_id, token, args)
            safety_decision = decision
            if not decision.accepted:
                return Response(ResponseKind.NONE)
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
                (" · Weather fallback data: Open-Meteo" if self.config.modules.env.enabled else "")
                + (
                    " · AI external provider: data leaves this node"
                    if self.config.modules.ai.enabled and self.config.ai.provider == "openai_compat"
                    else ""
                )
            ),
        )
        try:
            response = await spec.handler(ctx)
        except Exception:
            if safety_decision is not None and safety_decision.accepted:
                await self.rate_limiter.release_safety_floor(
                    member.mesh_id, token, safety_decision.fingerprint
                )
            return Response(ResponseKind.ERROR, [Line(message("internal_error"))])
        if response.kind == ResponseKind.ERROR and safety_decision is not None:
            await self.rate_limiter.release_safety_floor(
                member.mesh_id, token, safety_decision.fingerprint
            )
        return self.tui.activate(
            response,
            session,
            self.sessions.clock.monotonic(),
            direct=inbound.is_direct,
            fallback_title=spec.name,
        )
