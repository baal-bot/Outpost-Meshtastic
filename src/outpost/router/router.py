from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable

from outpost import __version__
from outpost.audit import write_audit
from outpost.commands.core import specs as core_specs
from outpost.config import Config
from outpost.render.catalogue import message
from outpost.security.rate_limit import SAFETY_FLOOR, RateLimiter
from outpost.store.members import MemberRepo
from outpost.transport.models import InboundMessage

from .channel_policy import CHANNEL_POLICY_REJECTIONS, decide
from .intents import TOLERANT_REJECTIONS, IntentResolver
from .models import (
    CommandContext,
    DispatchTrace,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
    TuiChoice,
    TuiScreen,
)
from .registry import CommandRegistry
from .session import Session, SessionStore
from .tui import TuiController

VERIFIED_MEMBER_MUTATIONS = frozenset({"FORGETPOS", "REMOVEME", "UPD"})


class Router:
    def __init__(
        self,
        config: Config,
        members: MemberRepo,
        sessions: SessionStore,
        rate_limiter: RateLimiter,
        node_name: Callable[[], str] | None = None,
    ) -> None:
        self.config, self.members, self.sessions, self.rate_limiter = (
            config,
            members,
            sessions,
            rate_limiter,
        )
        self.node_name = node_name or (lambda: self.config.node.name)
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

    def _clarify_command(
        self,
        invoked: str,
        candidates: tuple[str, ...],
        session: Session,
        inbound: InboundMessage,
    ) -> Response:
        _, separator, args = invoked.strip().partition(" ")
        if not inbound.is_direct:
            names = "/".join(candidates)
            return Response(
                ResponseKind.ERROR,
                [Line(f"Not run · send exact {names}, or DM ? for help.")],
            )
        title = "CONFIRM COMMAND" if len(candidates) == 1 else "CHOOSE COMMAND"
        response = Response(
            ResponseKind.ERROR,
            [Line("Nothing was run.")],
            screen=TuiScreen(
                "command-clarification",
                title,
                choices=tuple(
                    TuiChoice(
                        candidate,
                        f"{candidate}{' ' + args if separator else ''}",
                    )
                    for candidate in candidates
                ),
            ),
        )
        return self.tui.activate(
            response,
            session,
            self.sessions.clock.monotonic(),
            direct=True,
        )

    async def dispatch(
        self,
        inbound: InboundMessage,
        *,
        ordered: bool = True,
        trace: DispatchTrace | None = None,
    ) -> Response:
        try:
            async with asyncio.timeout(self.config.router.member_lock_timeout_s):
                if not ordered:
                    response = await self._dispatch_unlocked(inbound, trace)
                else:
                    lock = self._locks[inbound.from_id]
                    async with lock:
                        response = await self._dispatch_unlocked(inbound, trace)
        except TimeoutError:
            if trace is not None:
                trace.decision = "member_lock_timeout"
            response = Response(ResponseKind.ERROR, [Line(message("internal_error"))])
        if trace is not None:
            trace.response_kind = response.kind.value
        return response

    async def _dispatch_unlocked(
        self, inbound: InboundMessage, trace: DispatchTrace | None = None
    ) -> Response:
        invoked = self._invoked(inbound)
        if trace is not None:
            trace.input_command = invoked
        if invoked is None or inbound.no_reply:
            if trace is not None:
                trace.decision = "no_reply"
            return Response(ResponseKind.NONE)
        member = await self.members.resolve(
            inbound.from_id,
            last_heard_snr=inbound.rx_snr,
            hops_away=inbound.hops_away,
            authenticated_pki_key=(
                inbound.pki_public_key if inbound.is_direct and inbound.pki_encrypted else None
            ),
        )
        if trace is not None:
            trace.member_trust = member.trust
        if member.trust == "blocked":
            if trace is not None:
                trace.decision = "blocked_member"
            return Response(ResponseKind.NONE)
        channel = -1 if inbound.is_direct else inbound.channel
        session = self.sessions.get(member.mesh_id, channel)
        parts = invoked.split(maxsplit=1)
        token = parts[0] if parts else ""
        known = self.registry.known(token)
        if known is not None and self.registry.resolve(token) is None:
            if trace is not None:
                trace.resolved_command = known.name
                trace.decision = "module_disabled"
            self.tui.cancel_for_command(session)
            if not await self.rate_limiter.allow(member.mesh_id, member.trust, "COMMAND_REJECTED"):
                if trace is not None:
                    trace.decision = "rate_limited:module_disabled"
                return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
            TOLERANT_REJECTIONS.labels("module_disabled", known.name.lower()).inc()
            return Response(
                ResponseKind.ERROR,
                [Line(f"{known.name} unavailable · {known.module.title()} is disabled.")],
            )
        if self.registry.resolve(token) is not None:
            self.tui.cancel_for_command(session, preserve_operations=token.casefold() == "ops")
        else:
            invoked, pending_response = self.tui.prepare(
                invoked,
                session,
                self.sessions.clock.monotonic(),
                direct=inbound.is_direct,
            )
            if pending_response is not None:
                if trace is not None:
                    trace.decision = "tui_prompt"
                if not await self.rate_limiter.allow(member.mesh_id, member.trust, "MENU"):
                    if trace is not None:
                        trace.decision = "rate_limited:tui"
                    return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
                return pending_response
            parts = invoked.split(maxsplit=1)
            token = parts[0] if parts else ""
        if self.registry.resolve(token) is None:
            resolution = self.intents.resolve(
                invoked,
                member.trust,
                self.registry,
            )
            if resolution.candidates:
                if trace is not None:
                    trace.resolution = resolution.mode
                    trace.decision = "ambiguous"
                session.clear_operations_state()
                if not await self.rate_limiter.allow(
                    member.mesh_id, member.trust, "COMMAND_REJECTED"
                ):
                    if trace is not None:
                        trace.decision = "rate_limited:ambiguity"
                    return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
                return self._clarify_command(invoked, resolution.candidates, session, inbound)
            if resolution.mode == "mutation_protected":
                if trace is not None:
                    trace.resolution = resolution.mode
                    trace.decision = "mutation_protected"
                session.clear_operations_state()
                if not await self.rate_limiter.allow(
                    member.mesh_id, member.trust, "COMMAND_REJECTED"
                ):
                    if trace is not None:
                        trace.decision = "rate_limited:mutation_protected"
                    return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
                return Response(
                    ResponseKind.ERROR,
                    [Line("Command not run. Send ? for available actions.")],
                )
            invoked = resolution.invoked
            if trace is not None:
                trace.resolution = resolution.mode
            parts = invoked.split(maxsplit=1)
            token = parts[0] if parts else ""
        if self.registry.resolve(token) is not None:
            self.tui.cancel_for_command(session, preserve_operations=token.casefold() == "ops")
        args = parts[1] if len(parts) > 1 else ""
        if not await self.rate_limiter.allow(member.mesh_id, member.trust, token):
            if trace is not None:
                trace.resolved_command = token.upper() or None
                trace.decision = "rate_limited"
            return Response(ResponseKind.ERROR, [Line(message("rate_limited"))])
        spec = self.registry.resolve(token)
        if spec is None and inbound.is_direct and self.config.modules.ai.enabled:
            if session.tui_active:
                if trace is not None:
                    trace.decision = "tui_invalid_option"
                return Response(ResponseKind.ERROR, [Line("Not an option. Send ? for the menu.")])
            spec = self.registry.resolve("ASK")
            args = invoked
        if spec is None:
            if trace is not None:
                trace.decision = "unknown_command"
            session.clear_operations_state()
            return Response(ResponseKind.ERROR, [Line(message("unknown"))])
        if TrustLevel.parse(member.trust) < spec.min_trust:
            if trace is not None:
                trace.resolved_command = spec.name
                trace.decision = "insufficient_trust"
            return Response(ResponseKind.ERROR, [Line(message("unknown"))])
        if trace is not None:
            trace.resolved_command = spec.name
        policy = None if inbound.is_direct else self.config.channels.get(inbound.channel)
        channel_decision = decide(spec, direct=inbound.is_direct, policy=policy)
        if not channel_decision.allowed:
            if trace is not None:
                trace.decision = f"channel_policy:{channel_decision.reason}"
            CHANNEL_POLICY_REJECTIONS.labels(
                str(inbound.channel), spec.channel_use.value, channel_decision.reason
            ).inc()
            try:
                await write_audit(
                    self.members.database,
                    actor_kind="mesh",
                    actor_ref=member.mesh_id,
                    action="command.channel_policy_rejected",
                    target=f"channel/{inbound.channel}/{spec.channel_use.value}",
                    detail={"reason": channel_decision.reason},
                    created_at=int(self.members.clock.now().timestamp()),
                    outcome="denied",
                )
            except Exception as error:
                print(
                    f"Channel policy audit failed: {type(error).__name__}",
                    flush=True,
                )
            return Response(ResponseKind.ERROR, [Line(channel_decision.message)])
        requires_verified_identity = spec.name in VERIFIED_MEMBER_MUTATIONS
        if spec.min_trust >= TrustLevel.RESPONDER or requires_verified_identity:
            authorized, _reason = await self.members.authorize_elevated(member, inbound, spec.name)
            if not authorized:
                if trace is not None:
                    trace.decision = (
                        "verified_identity_denied"
                        if requires_verified_identity and spec.min_trust < TrustLevel.RESPONDER
                        else "elevated_identity_denied"
                    )
                session.clear_operations_state()
                prefix = (
                    "Verified action denied."
                    if requires_verified_identity and spec.min_trust < TrustLevel.RESPONDER
                    else "Elevated action denied."
                )
                return Response(
                    ResponseKind.ERROR,
                    [
                        Line(
                            f"{prefix} Use a verified PKI direct message or ask the operator to "
                            "review this radio key."
                        )
                    ],
                )
        safety_decision = None
        if token.upper() in SAFETY_FLOOR:
            decision = await self.rate_limiter.safety_floor_decision(member.mesh_id, token, args)
            safety_decision = decision
            if not decision.accepted:
                if trace is not None:
                    trace.decision = "safety_repeat_suppressed"
                return Response(ResponseKind.NONE)
        ctx = CommandContext(
            message=inbound,
            member=member,
            session=session,
            args=args,
            registry=self.registry,
            node_name=self.node_name(),
            operator_contact=self.config.node.operator_contact,
            version=__version__,
            disclaimer=self.config.node.disclaimer,
            channel_policy=policy,
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
        except asyncio.CancelledError:
            if trace is not None:
                trace.decision = "handler_cancelled"
            if safety_decision is not None and safety_decision.accepted:
                # A timeout/shutdown is not evidence that the report was recorded.
                # Apply the same retry policy as a handler error and propagate
                # cancellation. Service transactions own rollback/commit safety.
                await asyncio.shield(
                    self.rate_limiter.release_safety_floor(
                        member.mesh_id, token, safety_decision.fingerprint
                    )
                )
            raise
        except Exception:
            if trace is not None:
                trace.decision = "handler_error"
            if safety_decision is not None and safety_decision.accepted:
                await self.rate_limiter.release_safety_floor(
                    member.mesh_id, token, safety_decision.fingerprint
                )
            return Response(ResponseKind.ERROR, [Line(message("internal_error"))])
        if response.kind == ResponseKind.ERROR and safety_decision is not None:
            await self.rate_limiter.release_safety_floor(
                member.mesh_id, token, safety_decision.fingerprint
            )
        if response.max_parts is None:
            response.max_parts = spec.max_parts
        if trace is not None:
            trace.decision = "allowed" if response.kind != ResponseKind.ERROR else "handler_denied"
        return self.tui.activate(
            response,
            session,
            self.sessions.clock.monotonic(),
            direct=inbound.is_direct,
            fallback_title=spec.name,
        )
