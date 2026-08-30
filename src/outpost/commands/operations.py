from __future__ import annotations

import base64
import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import cast

from outpost.operations_center import MeshOperationsCenter
from outpost.router.models import (
    CommandContext,
    CommandHandler,
    CommandSpec,
    ContextFrame,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
    TuiChoice,
    TuiScreen,
)
from outpost.router.session import TuiConfirmation
from outpost.transport.models import TrafficClass

PAGE_SIZE = 3
SENSITIVE_SECONDS = 10 * 60


def _operator(ctx: CommandContext) -> bool:
    return TrustLevel.parse(ctx.member.trust) >= TrustLevel.OPERATOR


def _page(value: str) -> int | None:
    clean = value.strip() or "1"
    return int(clean) if clean.isdigit() and int(clean) > 0 else None


def _encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode(value: str) -> str | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded).decode()
    except (UnicodeDecodeError, ValueError):
        return None


def _enter(ctx: CommandContext, ref: str) -> None:
    if ctx.session.context and ctx.session.context[-1].kind == "OPS":
        ctx.session.context.pop()
    ctx.session.push(ContextFrame("OPS", ref))


def _screen(
    name: str,
    title: str,
    *,
    lines: list[Line] | None = None,
    choices: tuple[TuiChoice, ...] = (),
    input_command: str | None = None,
    input_prompt: str | None = None,
) -> Response:
    return Response(
        ResponseKind.DETAIL,
        lines or [],
        screen=TuiScreen(
            name,
            title,
            choices=choices,
            input_command=input_command,
            input_prompt=input_prompt,
            parent_command="OPS",
        ),
    )


def _time(value: object) -> str:
    try:
        return datetime.fromtimestamp(float(str(value))).strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "unknown time"


def specs(center: MeshOperationsCenter) -> list[CommandSpec]:
    async def snapshot_page(
        ctx: CommandContext,
        name: str,
        page_number: int,
        loader: Callable[[], Awaitable[list[str]]],
    ) -> tuple[list[str], int] | None:
        now = center.clock.monotonic()
        ctx.session.expire_tui_sensitive(now)
        if page_number == 1:
            ctx.session.tui_snapshots[name] = await loader()
            ctx.session.tui_snapshot_expires_at = now + SENSITIVE_SECONDS
        elif name not in ctx.session.tui_snapshots:
            return None
        refs = ctx.session.tui_snapshots[name]
        pages = max(1, math.ceil(len(refs) / PAGE_SIZE))
        if page_number > pages:
            return [], pages
        start = (page_number - 1) * PAGE_SIZE
        return refs[start : start + PAGE_SIZE], pages

    def paging_choices(command: str, page_number: int, pages: int) -> tuple[TuiChoice, ...]:
        choices: list[TuiChoice] = []
        if page_number > 1:
            choices.append(TuiChoice("Previous page", f"OPS {command} {page_number - 1}"))
        if page_number < pages:
            choices.append(TuiChoice("Next page", f"OPS {command} {page_number + 1}"))
        return tuple(choices)

    def confirm(
        ctx: CommandContext,
        *,
        action: str,
        target: str,
        payload: dict[str, str],
        title: str,
        label: str,
    ) -> Response:
        token = secrets.token_urlsafe(8)
        ctx.session.tui_confirmations.clear()
        ctx.session.tui_confirmations[token] = TuiConfirmation(
            action,
            target,
            payload,
            center.clock.monotonic() + SENSITIVE_SECONDS,
        )
        return _screen(
            f"ops-confirm-{action}",
            title,
            lines=[Line("Nothing changed yet. Confirmation expires in 10 minutes.")],
            choices=(
                TuiChoice(label, f"OPS DO {token}"),
                TuiChoice("Cancel", "OPS"),
            ),
        )

    async def home(ctx: CommandContext) -> Response:
        ctx.session.clear_operations_state()
        operator = _operator(ctx)
        value = await center.summary(operator=operator)
        _enter(ctx, "home")
        if operator:
            summary = (
                f"{value['action']} action · {value['incidents']} inc · welfare "
                f"{value['welfare_help']} help/{value['welfare_missing']} missing · "
                f"{value['inbox']} inbox · {value['failed']} failed"
            )
        else:
            summary = (
                f"{value['action']} action · {value['incidents']} inc · welfare "
                f"{value['welfare_help']} help/{value['welfare_missing']} missing · "
                f"{value['failed']} failed"
            )
        choices = [
            TuiChoice("Action needed", "OPS ACTION"),
            TuiChoice("Incidents", "OPS INCS"),
            TuiChoice("Welfare", "OPS WELFARE"),
        ]
        if operator:
            choices.append(TuiChoice("Inbox", "OPS INBOX"))
        choices.append(TuiChoice("Delivery failures", "OPS FAIL"))
        return _screen("ops-home", "OPS", lines=[Line(summary)], choices=tuple(choices))

    async def action_needed(ctx: CommandContext) -> Response:
        operator = _operator(ctx)
        value = await center.summary(operator=operator)
        _enter(ctx, "action needed")
        choices: list[TuiChoice] = []
        if value["incident_action"]:
            choices.append(TuiChoice(f"Incidents · {value['incident_action']}", "OPS INCS"))
        if int(value["welfare_help"]) or int(value["welfare_missing"]):
            choices.append(
                TuiChoice(
                    f"Welfare · {value['welfare_help']} help/{value['welfare_missing']} missing",
                    "OPS WELFARE",
                )
            )
        if int(value["failed"]):
            choices.append(TuiChoice(f"Delivery failures · {value['failed']}", "OPS FAIL"))
        if operator and int(value["inbox"]):
            choices.append(TuiChoice(f"Inbox · {value['inbox']}", "OPS INBOX"))
        if operator and int(value["federation"]):
            choices.append(TuiChoice(f"Federation reviews · {value['federation']}", "OPS REVIEWS"))
        if not choices:
            return _screen(
                "ops-action", "ACTION NEEDED", lines=[Line("No actionable work right now.")]
            )
        return _screen(
            "ops-action",
            "ACTION NEEDED",
            lines=[Line(f"{value['action']} items need review; choose a queue.")],
            choices=tuple(choices),
        )

    async def incident_list(ctx: CommandContext, value: str) -> Response:
        page_number = _page(value)
        if page_number is None:
            return Response(ResponseKind.ERROR, [Line("Use OPS INCS [page].")])
        page = await snapshot_page(ctx, "incidents", page_number, center.incident_refs)
        if page is None:
            return Response(ResponseKind.ERROR, [Line("Incident list expired. Send OPS INCS.")])
        refs, pages = page
        _enter(ctx, f"incidents page {page_number}")
        choices: list[TuiChoice] = []
        for ref in refs:
            item = await center.incident(int(ref))
            if item is not None:
                choices.append(
                    TuiChoice(
                        f"INC {item['local_ref']} · {str(item['severity']).upper()} · "
                        f"{str(item['title'])[:22]}",
                        f"OPS INC {item['id']}",
                    )
                )
        choices.extend(paging_choices("INCS", page_number, pages))
        line = (
            f"Page {page_number}/{pages} · metadata only; no coordinates."
            if choices
            else "No active incidents."
        )
        return _screen("ops-incidents", "OPS INCIDENTS", lines=[Line(line)], choices=tuple(choices))

    async def incident_detail(ctx: CommandContext, value: str) -> Response:
        if not value.isdigit() or (item := await center.incident(int(value))) is None:
            return Response(ResponseKind.ERROR, [Line("Incident not found.")])
        _enter(ctx, f"incident {item['local_ref']}")
        choices: list[TuiChoice] = []
        if ctx.registry.resolve("ACK") is not None and item["status"] in {"open", "monitoring"}:
            choices.append(TuiChoice("Acknowledge incident", f"ACK {item['local_ref']}"))
        if _operator(ctx) and item["status"] in {"open", "monitoring"}:
            choices.append(TuiChoice("Resolve incident", f"OPS RESOLVE {item['id']}"))
        return _screen(
            "ops-incident",
            f"INCIDENT {item['local_ref']}",
            lines=[
                Line(f"{str(item['severity']).upper()} · {item['type']} · {item['status']}"),
                Line(str(item["title"])[:80]),
                Line(
                    f"Confirmed {item['confirm_count']} · disputed {item['dispute_count']} · "
                    f"updated {_time(item['updated_at'])}"
                ),
            ],
            choices=tuple(choices),
        )

    async def welfare(ctx: CommandContext, value: str) -> Response:
        page_number = _page(value)
        if page_number is None:
            return Response(ResponseKind.ERROR, [Line("Use OPS WELFARE [page].")])
        page = await snapshot_page(ctx, "welfare", page_number, center.welfare_refs)
        if page is None:
            return Response(ResponseKind.ERROR, [Line("Welfare list expired. Send OPS WELFARE.")])
        refs, pages = page
        current = await center.welfare()
        if current is None:
            return _screen("ops-welfare", "OPS WELFARE", lines=[Line("No open welfare event.")])
        _enter(ctx, f"welfare page {page_number}")
        by_id = {str(item["id"]): item for item in current["items"]}
        choices = [
            TuiChoice(
                f"@{by_id[ref]['handle'] or by_id[ref]['mesh_id']} · "
                f"{str(by_id[ref]['status']).replace('_', ' ').upper()}",
                f"OPS PERSON {ref}",
            )
            for ref in refs
            if ref in by_id
        ]
        choices.extend(paging_choices("WELFARE", page_number, pages))
        if _operator(ctx):
            choices.append(TuiChoice("Close welfare event", f"OPS CLOSE {current['event']['id']}"))
        counts = current["counts"]
        return _screen(
            "ops-welfare",
            "OPS WELFARE",
            lines=[
                Line(f"{str(current['event']['name'])[:36]} · page {page_number}/{pages}"),
                Line(
                    f"{counts['ok']} ok · {counts['need_help']} help · "
                    f"{counts['unaccounted']} missing"
                ),
            ],
            choices=tuple(choices),
        )

    async def welfare_person(ctx: CommandContext, value: str) -> Response:
        current = await center.welfare()
        if current is None or not value.isdigit():
            return Response(ResponseKind.ERROR, [Line("Welfare status not found.")])
        item = next((row for row in current["items"] if row["id"] == int(value)), None)
        if item is None:
            return Response(ResponseKind.ERROR, [Line("Welfare status not found.")])
        _enter(ctx, "welfare member status")
        label = item["handle"] or item["mesh_id"]
        return _screen(
            "ops-welfare-person",
            "WELFARE STATUS",
            lines=[
                Line(f"@{label} · {str(item['status']).replace('_', ' ').upper()}"),
                Line(f"Last status: {_time(item['created_at'])} · notes and coordinates withheld."),
            ],
        )

    async def inbox_list(ctx: CommandContext, value: str) -> Response:
        if not _operator(ctx):
            return Response(ResponseKind.ERROR, [Line("Operator role required; nothing changed.")])
        page_number = _page(value)
        if page_number is None:
            return Response(ResponseKind.ERROR, [Line("Use OPS INBOX [page].")])
        page = await snapshot_page(ctx, "inbox", page_number, center.conversation_refs)
        if page is None:
            return Response(ResponseKind.ERROR, [Line("Inbox list expired. Send OPS INBOX.")])
        refs, pages = page
        _enter(ctx, f"inbox page {page_number}")
        choices: list[TuiChoice] = []
        for ref in refs:
            item = await center.conversation(ref)
            if item is not None:
                choices.append(
                    TuiChoice(
                        f"@{str(item['participant_handle'])[:12]} · "
                        f"{str(item['subject'])[:25]} · {item['unread_count']} new",
                        f"OPS MSG {_encode(ref)}",
                    )
                )
        choices.extend(paging_choices("INBOX", page_number, pages))
        return _screen(
            "ops-inbox",
            "OPS INBOX",
            lines=[
                Line(
                    f"Page {page_number}/{pages} · metadata only; message bodies withheld."
                    if choices
                    else "No active operations conversations."
                )
            ],
            choices=tuple(choices),
        )

    async def message_detail(ctx: CommandContext, encoded: str) -> Response:
        if not _operator(ctx):
            return Response(ResponseKind.ERROR, [Line("Conversation not found or not allowed.")])
        key = _decode(encoded)
        item = await center.conversation(key) if key is not None else None
        if item is None:
            return Response(ResponseKind.ERROR, [Line("Conversation not found or not allowed.")])
        _enter(ctx, "inbox conversation metadata")
        choices = [TuiChoice("Archive conversation", f"OPS ARCHIVE {encoded}")]
        if item["reply_available"] and center.reply_sender is not None:
            choices.append(TuiChoice("Reply", f"OPS REPLY {encoded}"))
        route = str(item["peer_name"] or item["peer_mesh_id"] or item["route_kind"])
        return _screen(
            "ops-message",
            "CONVERSATION",
            lines=[
                Line(f"@{item['participant_handle']} · {str(item['subject'])[:70]}"),
                Line(
                    f"{item['message_count']} messages · {item['unread_count']} unread · "
                    f"{item['failed_count']} failed"
                ),
                Line(f"Route: {route[:45]} · message bodies withheld."),
            ],
            choices=tuple(choices),
        )

    async def failure_list(ctx: CommandContext, value: str) -> Response:
        page_number = _page(value)
        if page_number is None:
            return Response(ResponseKind.ERROR, [Line("Use OPS FAIL [page].")])
        page = await snapshot_page(ctx, "failures", page_number, center.failure_refs)
        if page is None:
            return Response(ResponseKind.ERROR, [Line("Failure list expired. Send OPS FAIL.")])
        refs, pages = page
        _enter(ctx, f"delivery failures page {page_number}")
        choices: list[TuiChoice] = []
        for ref in refs:
            item = await center.failure(int(ref))
            if item is not None:
                choices.append(
                    TuiChoice(
                        f"#{item['id']} · {item['traffic_class']} → {item['destination']}",
                        f"OPS FAILURE {item['id']}",
                    )
                )
        choices.extend(paging_choices("FAIL", page_number, pages))
        return _screen(
            "ops-failures",
            "DELIVERY FAILURES",
            lines=[
                Line(
                    f"Page {page_number}/{pages} · payloads and internal errors withheld."
                    if choices
                    else "No failed deliveries."
                )
            ],
            choices=tuple(choices),
        )

    async def failure_detail(ctx: CommandContext, value: str) -> Response:
        item = await center.failure(int(value)) if value.isdigit() else None
        if item is None or item["state"] != "failed":
            return Response(ResponseKind.ERROR, [Line("Failed delivery not found.")])
        _enter(ctx, f"delivery failure {item['id']}")
        return _screen(
            "ops-failure",
            f"DELIVERY {item['id']}",
            lines=[
                Line(f"{item['traffic_class']} → {item['destination']} · ch {item['channel']}"),
                Line(str(item["outcome_explanation"])),
                Line(
                    f"Reason {item['reason_code']} · {item['attempts']} attempts · "
                    f"{_time(item['outcome_at'])}"
                ),
            ],
        )

    async def review_list(ctx: CommandContext, value: str) -> Response:
        if not _operator(ctx):
            return Response(ResponseKind.ERROR, [Line("Operator role required; nothing changed.")])
        page_number = _page(value)
        if page_number is None:
            return Response(ResponseKind.ERROR, [Line("Use OPS REVIEWS [page].")])
        page = await snapshot_page(ctx, "reviews", page_number, center.federation_refs)
        if page is None:
            return Response(ResponseKind.ERROR, [Line("Review list expired. Send OPS REVIEWS.")])
        refs, pages = page
        _enter(ctx, f"federation reviews page {page_number}")
        choices: list[TuiChoice] = []
        for ref in refs:
            item = await center.federation_item(int(ref))
            if item is not None:
                choices.append(
                    TuiChoice(
                        f"{item['stream']} · {str(item['node_name'] or item['mesh_id'])[:22]}",
                        f"OPS REVIEW {item['id']}",
                    )
                )
        choices.extend(paging_choices("REVIEWS", page_number, pages))
        return _screen(
            "ops-reviews",
            "FEDERATION REVIEWS",
            lines=[
                Line(
                    f"Page {page_number}/{pages} · quarantined metadata only."
                    if choices
                    else "No federation records await review."
                )
            ],
            choices=tuple(choices),
        )

    async def review_detail(ctx: CommandContext, value: str) -> Response:
        if not _operator(ctx):
            return Response(
                ResponseKind.ERROR, [Line("Federation review not found or not allowed.")]
            )
        item = await center.federation_item(int(value)) if value.isdigit() else None
        if item is None:
            return Response(
                ResponseKind.ERROR, [Line("Federation review not found or not allowed.")]
            )
        _enter(ctx, f"federation review {item['id']}")
        return _screen(
            "ops-review",
            "FEDERATION REVIEW",
            lines=[
                Line(f"{item['stream']} · {str(item['node_name'] or item['mesh_id'])[:45]}"),
                Line(f"Received {_time(item['received_at'])} · content withheld until import."),
            ],
            choices=(TuiChoice("Approve import", f"OPS IMPORT {item['id']}"),),
        )

    async def operator_confirmation(ctx: CommandContext, verb: str, value: str) -> Response:
        if not _operator(ctx):
            return Response(ResponseKind.ERROR, [Line("Operator role required; nothing changed.")])
        if verb == "RESOLVE":
            item = await center.incident(int(value)) if value.isdigit() else None
            if item is None or item["status"] not in {"open", "monitoring"}:
                return Response(ResponseKind.ERROR, [Line("Active incident not found.")])
            return _screen(
                "ops-resolve-note",
                f"RESOLVE INC {item['local_ref']}",
                lines=[Line("A resolution note is required; nothing has changed.")],
                input_command=f"OPS RESOLVENOTE {item['id']}",
                input_prompt="Send a brief resolution note",
            )
        if verb == "CLOSE":
            current = await center.welfare()
            if current is None or str(current["event"]["id"]) != value:
                return Response(ResponseKind.ERROR, [Line("Open welfare event not found.")])
            return confirm(
                ctx,
                action="close_event",
                target=value,
                payload={},
                title="CLOSE WELFARE EVENT?",
                label="Confirm close event",
            )
        if verb == "ARCHIVE":
            key = _decode(value)
            if key is None or await center.conversation(key) is None:
                return Response(ResponseKind.ERROR, [Line("Conversation not found.")])
            return confirm(
                ctx,
                action="archive",
                target=key,
                payload={},
                title="ARCHIVE CONVERSATION?",
                label="Confirm archive",
            )
        if verb == "IMPORT":
            item = await center.federation_item(int(value)) if value.isdigit() else None
            if item is None:
                return Response(ResponseKind.ERROR, [Line("Pending federation item not found.")])
            return confirm(
                ctx,
                action="import",
                target=value,
                payload={},
                title="IMPORT FEDERATION ITEM?",
                label="Confirm approved import",
            )
        if verb == "REPLY":
            key = _decode(value)
            item = await center.conversation(key) if key is not None else None
            if item is None or not item["reply_available"]:
                return Response(ResponseKind.ERROR, [Line("Safe reply route not found.")])
            return _screen(
                "ops-reply-body",
                f"REPLY TO @{str(item['participant_handle'])[:12]}",
                lines=[Line("Reply is stored and operator-audited; nothing sent yet.")],
                input_command=f"OPS REPLYDRAFT {value}",
                input_prompt="Send reply text, up to 200 bytes",
            )
        return Response(ResponseKind.ERROR, [Line("Unknown operator action; nothing changed.")])

    async def resolution_note(ctx: CommandContext, value: str) -> Response:
        incident_id, separator, note = value.strip().partition(" ")
        if not _operator(ctx) or not incident_id.isdigit() or not separator or not note.strip():
            return Response(ResponseKind.ERROR, [Line("A resolution note is required.")])
        clean = note.strip()
        if len(clean.encode()) > 200:
            return Response(ResponseKind.ERROR, [Line("Resolution note must be 1-200 bytes.")])
        item = await center.incident(int(incident_id))
        if item is None or item["status"] not in {"open", "monitoring"}:
            return Response(ResponseKind.ERROR, [Line("Active incident not found.")])
        return confirm(
            ctx,
            action="resolve",
            target=incident_id,
            payload={"resolution": clean},
            title=f"RESOLVE INC {item['local_ref']}?",
            label="Confirm resolved",
        )

    async def reply_draft(ctx: CommandContext, value: str) -> Response:
        encoded, separator, body = value.strip().partition(" ")
        key = _decode(encoded)
        clean = body.strip()
        if not _operator(ctx) or key is None or not separator or not clean:
            return Response(ResponseKind.ERROR, [Line("Reply text is required.")])
        if len(clean.encode()) > 200:
            return Response(ResponseKind.ERROR, [Line("Reply must be 1-200 bytes.")])
        item = await center.conversation(key)
        if item is None or not item["reply_available"]:
            return Response(ResponseKind.ERROR, [Line("Safe reply route not found.")])
        return confirm(
            ctx,
            action="reply",
            target=key,
            payload={"body": clean},
            title="SEND OPERATIONS REPLY?",
            label=f"Confirm reply · {len(clean.encode())} bytes",
        )

    async def execute(ctx: CommandContext, token: str) -> Response:
        if not _operator(ctx):
            return Response(ResponseKind.ERROR, [Line("Operator role required; nothing changed.")])
        ctx.session.expire_tui_sensitive(center.clock.monotonic())
        confirmation = ctx.session.tui_confirmations.pop(token, None)
        if confirmation is None:
            return Response(
                ResponseKind.ERROR,
                [Line("Confirmation expired, already used, or interrupted. Nothing changed.")],
            )
        try:
            if confirmation.action == "resolve":
                result = await center.resolve_incident(
                    int(confirmation.target), confirmation.payload["resolution"], ctx.member.mesh_id
                )
            elif confirmation.action == "close_event":
                result = await center.close_event(int(confirmation.target), ctx.member.mesh_id)
            elif confirmation.action == "archive":
                result = await center.archive_conversation(confirmation.target, ctx.member.mesh_id)
            elif confirmation.action == "import":
                result = await center.import_federation_item(
                    int(confirmation.target), ctx.member.mesh_id
                )
            elif confirmation.action == "reply":
                result = await center.reply(
                    confirmation.target, confirmation.payload["body"], ctx.member.mesh_id
                )
            else:
                raise ValueError("Unknown confirmed action.")
        except (KeyError, TypeError, ValueError) as error:
            return Response(ResponseKind.ERROR, [Line(f"Not completed: {error}")])
        ctx.session.tui_snapshots.clear()
        return _screen(
            "ops-complete",
            "OPS ACTION COMPLETE",
            lines=[Line(result)],
            choices=(TuiChoice("Operations home", "OPS"),),
        )

    async def ops(ctx: CommandContext) -> Response:
        verb, _, remainder = ctx.args.strip().partition(" ")
        verb = verb.upper()
        if verb != "DO":
            ctx.session.tui_confirmations.clear()
        if not verb or verb in {"HOME", "STATUS"}:
            return await home(ctx)
        if verb == "ACTION":
            return await action_needed(ctx)
        if verb == "INCS":
            return await incident_list(ctx, remainder)
        if verb == "INC":
            return await incident_detail(ctx, remainder.strip())
        if verb == "WELFARE":
            return await welfare(ctx, remainder)
        if verb == "PERSON":
            return await welfare_person(ctx, remainder.strip())
        if verb == "INBOX":
            return await inbox_list(ctx, remainder)
        if verb == "MSG":
            return await message_detail(ctx, remainder.strip())
        if verb == "FAIL":
            return await failure_list(ctx, remainder)
        if verb == "FAILURE":
            return await failure_detail(ctx, remainder.strip())
        if verb == "REVIEWS":
            return await review_list(ctx, remainder)
        if verb == "REVIEW":
            return await review_detail(ctx, remainder.strip())
        if verb in {"RESOLVE", "CLOSE", "ARCHIVE", "IMPORT", "REPLY"}:
            return await operator_confirmation(ctx, verb, remainder.strip())
        if verb == "RESOLVENOTE":
            return await resolution_note(ctx, remainder)
        if verb == "REPLYDRAFT":
            return await reply_draft(ctx, remainder)
        if verb == "DO":
            return await execute(ctx, remainder.strip())
        return Response(ResponseKind.ERROR, [Line("Use OPS or choose a numbered action.")])

    return [
        CommandSpec(
            "OPS",
            (),
            module="operations",
            min_trust=TrustLevel.RESPONDER,
            airtime_class=TrafficClass.REPLY,
            max_parts=3,
            rate_key="commands",
            help_short=(
                "OPS [ACTION|INCS|WELFARE|INBOX|FAIL|REVIEWS] · PKI-authenticated action center"
            ),
            handler=cast(CommandHandler, ops),
            mutates=True,
        )
    ]
