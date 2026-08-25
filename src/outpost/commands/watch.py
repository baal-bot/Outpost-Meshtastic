from __future__ import annotations

from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.models import TrafficClass
from outpost.watch.incidents import IncidentService


def specs(service: IncidentService) -> list[CommandSpec]:
    async def report(ctx: CommandContext) -> Response:
        try:
            created, similar = await service.create(ctx.args, ctx.member)
        except ValueError as error:
            return Response(ResponseKind.ERROR, [Line(str(error))])
        if similar:
            return Response(
                ResponseKind.DETAIL,
                [
                    Line(
                        f"Similar: INC {similar.local_ref} {similar.type} "
                        f"{similar.title[:24]}. CONFIRM {similar.local_ref}, "
                        "or REPORT! to file new."
                    )
                ],
            )
        assert created is not None
        if created.lat is None:
            text = (
                f"✓ INC {created.local_ref} {created.type}. No location — "
                f"send UPD {created.local_ref} <where>."
            )
        else:
            text = (
                f"✓ INC {created.local_ref} {created.type} · "
                f"GPS {created.lat:.3f},{created.lon:.3f}"
            )
        return Response(ResponseKind.ACK, [Line(text)])

    async def report_force(ctx: CommandContext) -> Response:
        created, _ = await service.create(ctx.args, ctx.member, force=True)
        assert created is not None
        return Response(
            ResponseKind.ACK, [Line(f"✓ INC {created.local_ref} {created.type} filed.")]
        )

    async def confirm(ctx: CommandContext) -> Response:
        token = ctx.args.strip()
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("CONFIRM needs incident number.")])
        value = await service.react(int(token), ctx.member, "confirm")
        return Response(
            ResponseKind.ACK, [Line(f"✓ INC {value.local_ref} confirmed · ✓{value.confirm_count}")]
        )

    async def dispute(ctx: CommandContext) -> Response:
        token, _, note = ctx.args.strip().partition(" ")
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("DISPUTE needs incident number [note].")])
        value = await service.react(int(token), ctx.member, "dispute", note)
        return Response(
            ResponseKind.ACK, [Line(f"✓ INC {value.local_ref} disputed · {value.dispute_count}")]
        )

    async def incidents(ctx: CommandContext) -> Response:
        values = await service.list(limit=5)
        if not values:
            return Response(ResponseKind.LISTING, [Line("No active incidents.")])
        lines = [Line(f"{len(values)} active · no position")]
        markers = {"critical": "⚠⚠", "urgent": "⚠", "caution": "!", "info": ""}
        lines.extend(
            Line(
                f"{item.local_ref} {markers[item.severity]} {item.type} "
                f"{item.title[:28]} ✓{item.confirm_count}"
            )
            for item in values
        )
        return Response(ResponseKind.LISTING, lines)

    async def detail(ctx: CommandContext) -> Response:
        token = ctx.args.strip()
        if not token.isdigit():
            return Response(ResponseKind.ERROR, [Line("INC needs incident number.")])
        value = await service.by_ref(int(token))
        if value is None:
            return Response(ResponseKind.ERROR, [Line("No incident.")])
        updates = await service.updates(value.id)
        lines = [
            Line(f"INC {value.local_ref} · {value.severity} {value.type} · {value.status}"),
            Line(value.body or value.title),
            Line(
                f"{value.location_text or 'No location'} · "
                f"✓{value.confirm_count} ?{value.dispute_count}"
            ),
        ]
        lines.extend(
            Line(f"{update['kind']} @{update['author_label']}: {update['body'] or 'noted'}")
            for update in updates
        )
        return Response(ResponseKind.DETAIL, lines)

    base = dict(
        module="watch",
        min_trust=TrustLevel.GUEST,
        airtime_class=TrafficClass.REPLY,
        max_parts=3,
        rate_key="incidents",
    )
    return [
        CommandSpec(
            "REPORT",
            (),
            help_short=(
                "REPORT [-nopos|-wp name] <details> · files a public incident; "
                "attached position is visible. Not 911."
            ),
            handler=report,
            **base,
        ),
        CommandSpec(
            "REPORT!",
            (),
            help_short="REPORT! <details> · file despite duplicate suggestion",
            handler=report_force,
            **base,
        ),
        CommandSpec(
            "CONFIRM",
            (),
            help_short="CONFIRM <inc> · independently confirm",
            handler=confirm,
            **base,
        ),
        CommandSpec(
            "DISPUTE", (), help_short="DISPUTE <inc> [note] · flag concern", handler=dispute, **base
        ),
        CommandSpec(
            "INCIDENTS",
            ("INCS",),
            help_short="INCIDENTS · active incident list",
            handler=incidents,
            **base,
        ),
        CommandSpec(
            "INC", ("I",), help_short="INC <number> · incident detail", handler=detail, **base
        ),
    ]
