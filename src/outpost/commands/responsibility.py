"""Bounded verified-DM responsibility workflow, through the existing router/governor."""

from __future__ import annotations

import re
from typing import Any, cast

from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
    TuiChoice,
    TuiScreen,
)
from outpost.transport.chunker import truncate_utf8
from outpost.transport.models import TrafficClass
from outpost.watch.incidents import IncidentService
from outpost.watch.responsibility import Action, ResponsibilityActor, TargetKind

HELP = (
    "TASK <inc> [NEXT|TARGETS member|group|account [after]|<action> <token> [kind:id next action]]"
)


def specs(incidents: IncidentService) -> list[CommandSpec]:
    async def task(context: CommandContext) -> Response:
        ctx = context
        service = incidents.responsibility
        try:
            if (
                not ctx.message.is_direct
                or not ctx.message.pki_encrypted
                or len(ctx.args.encode()) > 220
            ):
                raise ValueError("Use a verified PKI DM, at most 220 argument bytes.")
            parts = ctx.args.split(maxsplit=1)
            if (
                not parts
                or not re.fullmatch(r"[0-9]{1,19}", parts[0])
                or not 0 < int(parts[0]) < 2**63
            ):
                raise ValueError(HELP)
            incident = await incidents.by_ref(int(parts[0]))
            if incident is None:
                raise ValueError("Incident not found.")
            actor = ResponsibilityActor(
                member_id=ctx.member.id, public_key=ctx.message.pki_public_key
            )
            view = await service.snapshot(incident.id, actor)
            remainder = parts[1] if len(parts) > 1 else ""
            command, _, arguments = remainder.partition(" ")
            if command.upper() == "TARGETS":
                target_parts = arguments.split()
                if (
                    not target_parts
                    or target_parts[0] not in {"member", "group", "account"}
                    or len(target_parts) > 2
                ):
                    raise ValueError("TASK <inc> TARGETS member|group|account [after]")
                if len(target_parts) > 1 and not re.fullmatch(r"[0-9]{1,19}", target_parts[1]):
                    raise ValueError("Invalid target page.")
                kind = cast(TargetKind, target_parts[0])
                after = int(target_parts[1]) if len(target_parts) > 1 else 0
                targets = await service.targets(kind, after=after, limit=3)
                lines = [
                    Line(f"{kind}:{t['reference']} {truncate_utf8(t['label'], 30)}")
                    for t in targets
                ]
                if len(targets) == 3:
                    lines.append(
                        Line(
                            f"More: TASK {incident.local_ref} TARGETS "
                            f"{kind} {targets[-1]['reference']}"
                        )
                    )
                return Response(ResponseKind.LISTING, lines or [Line("No more eligible targets.")])
            if command.upper() == "NEXT":
                return Response(
                    ResponseKind.DETAIL,
                    [
                        Line(view["next_action"] or "No accepted next action."),
                        Line("Offer: " + (view["offer_action"] or "none")),
                    ],
                )
            if command:
                decision = arguments.split(maxsplit=1)
                if not decision:
                    raise ValueError("Use the current TASK token. " + HELP)
                token = decision[0]
                extra = decision[1] if len(decision) > 1 else ""
                action = cast(Action, command.lower())
                kind_value: TargetKind | None = None
                reference: int | None = None
                next_action: str | None = None
                if action == "offer":
                    target, _, next_action = extra.partition(" ")
                    match = re.fullmatch(r"(member|group|account):([0-9]{1,19})", target)
                    if match is None or not 0 < int(match[2]) < 2**63:
                        raise ValueError("Offer needs kind:id and a next action.")
                    kind_value, reference = cast(TargetKind, match[1]), int(match[2])
                elif action == "update":
                    next_action = extra
                elif extra:
                    raise ValueError("Unexpected action arguments.")
                await service.apply(
                    incident.id,
                    actor,
                    action,
                    token,
                    target_kind=kind_value,
                    target_ref=reference,
                    next_action=next_action,
                )
                view = await service.snapshot(incident.id, actor)
            owner, offer = view["owner"], view["offer"]

            def label(target: dict[str, Any]) -> str:
                return truncate_utf8(target["label"], 26) + (
                    " UNAVAILABLE" if not target["available"] else ""
                )

            lines = [
                Line(f"TASK {incident.local_ref} LOCAL · {view['state']}"),
                Line("Owner: " + (label(owner) if owner else "none")),
                Line("Pending acceptance: " + (label(offer) if offer else "none")),
                Line(f"Verified: {view['verification']} · ACK is not acceptance."),
                Line(f"Token {view['review_token']}"),
            ]
            choices = [TuiChoice("Next action", f"TASK {incident.local_ref} NEXT")]
            for action in ("accept", "cancel", "release", "complete"):
                if action in view["allowed_actions"]:
                    choices.append(
                        TuiChoice(
                            action.title(),
                            f"TASK {incident.local_ref} {action} {view['review_token']}",
                        )
                    )
            return Response(
                ResponseKind.DETAIL,
                lines,
                screen=TuiScreen(
                    "responsibility",
                    "LOCAL RESPONSIBILITY",
                    choices=tuple(choices),
                    parent_command=f"INC {incident.local_ref}",
                ),
            )
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])

    return [
        CommandSpec(
            "TASK",
            (),
            "watch",
            TrustLevel.RESPONDER,
            TrafficClass.REPLY,
            3,
            "responsibility",
            HELP,
            True,
            task,
        )
    ]
