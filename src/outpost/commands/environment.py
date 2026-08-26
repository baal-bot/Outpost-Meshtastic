from __future__ import annotations

from outpost.config import Config
from outpost.env import (
    AstronomyService,
    CapAlertService,
    SeismicService,
    WaypointService,
    WeatherService,
)
from outpost.router.models import (
    CommandContext,
    CommandSpec,
    Line,
    Response,
    ResponseKind,
    TrustLevel,
)
from outpost.transport.models import TrafficClass


def _fit_radio(text: str, limit: int = 200) -> str:
    if len(text.encode()) <= limit:
        return text
    suffix = "…"
    while text and len((text + suffix).encode()) > limit:
        text = text[:-1]
    return text.rstrip() + suffix


def specs(
    service: WeatherService,
    config: Config,
    cap_alerts: CapAlertService | None = None,
    astronomy: AstronomyService | None = None,
    seismic: SeismicService | None = None,
    waypoints: WaypointService | None = None,
) -> list[CommandSpec]:
    async def weather(ctx: CommandContext) -> Response:
        location = config.node.location
        if location is None:
            return Response(
                ResponseKind.ERROR, [Line("WX unavailable · Outpost location not set.")]
            )
        mode = ctx.args.strip().upper()
        if mode and mode not in {"TODAY", "TOMORROW", "HOURLY"}:
            return Response(
                ResponseKind.ERROR, [Line("Use WX, WX TODAY, WX TOMORROW, or WX HOURLY.")]
            )
        try:
            if mode:
                forecast = await service.forecast(location.lat, location.lon)
            else:
                value = await service.current(location.lat, location.lon)
        except RuntimeError:
            return Response(
                ResponseKind.ERROR, [Line("WX unavailable · no safe cached conditions.")]
            )
        imperial = config.node.units == "imperial"

        def temp(value: float | None) -> str:
            if value is None:
                return "—"
            converted = value * 9 / 5 + 32 if imperial else value
            return f"{converted:.0f}°{'F' if imperial else 'C'}"

        def optional_float(value: object) -> float | None:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        if mode in {"TODAY", "TOMORROW"}:
            index = 0 if mode == "TODAY" else 1
            if len(forecast.daily) <= index:
                return Response(ResponseKind.ERROR, [Line(f"WX {mode.lower()} unavailable.")])
            day = forecast.daily[index]
            raw_wind = day.get("wind_kph")
            wind = (
                (float(raw_wind) / 1.609344 if imperial else float(raw_wind))
                if raw_wind is not None
                else None
            )
            rain = day.get("precipitation_probability")
            cached = " cached" if forecast.stale else ""
            rain_text = f"{rain}%" if rain is not None else "—"
            wind_text = f"{wind:.0f}{'mph' if imperial else 'km/h'}" if wind is not None else "—"
            text = (
                f"{mode.title()} {temp(optional_float(day.get('high_c')))}/"
                f"{temp(optional_float(day.get('low_c')))} · {day['summary']} · "
                f"rain {rain_text} · wind {wind_text}{cached}"
            )
            return Response(ResponseKind.DETAIL, [Line(text)])
        if mode == "HOURLY":
            periods = forecast.hourly[:6]

            def hourly_text(period: dict[str, object]) -> str:
                rain = period.get("precipitation_probability")
                rain_text = f"{rain}%" if rain is not None else "—"
                return (
                    f"{str(period['start_time'])[11:16]} "
                    f"{temp(optional_float(period.get('temperature_c')))} {rain_text}"
                )

            values = " · ".join(hourly_text(period) for period in periods)
            return Response(ResponseKind.DETAIL, [Line(f"Next hours · {values}")])
        age_seconds = value.valid_age_seconds
        age = (
            "time unknown"
            if age_seconds is None
            else "now"
            if age_seconds < 60
            else f"{age_seconds // 60}m"
        )
        stale = " cached" if value.stale else ""
        kind = {
            "observation": "observed",
            "forecast": "forecast",
            "estimate": "model",
            "peer": "peer",
        }.get(value.source_kind, value.source_kind)
        measurements = [f"WX {temp(value.temperature_c)}"]
        if value.apparent_c is not None:
            measurements.append(f"feels {temp(value.apparent_c)}")
        if value.wind_kph is not None:
            wind = value.wind_kph / 1.609344 if imperial else value.wind_kph
            direction = f" {value.wind_direction}°" if value.wind_direction is not None else ""
            measurements.append(f"wind {wind:.0f}{'mph' if imperial else 'km/h'}{direction}")
        elif value.temperature_c is None:
            measurements.append("measurements unavailable")
        text = f"{' · '.join(measurements)} · {value.provider} {kind}{stale} · valid {age}"
        return Response(ResponseKind.DETAIL, [Line(_fit_radio(text))])

    async def forecast(ctx: CommandContext) -> Response:
        location = config.node.location
        if location is None:
            return Response(
                ResponseKind.ERROR, [Line("FC unavailable · Outpost location not set.")]
            )
        tokens = ctx.args.lower().split()
        long = "-long" in tokens
        values = [argument for argument in tokens if argument != "-long"]
        if len(values) > 1 or (values and not values[0].isdigit()):
            return Response(ResponseKind.ERROR, [Line("Use FC [1-5] [-long].")])
        days = int(values[0]) if values else 3
        if days not in range(1, 6):
            return Response(ResponseKind.ERROR, [Line("FC days must be 1-5.")])
        try:
            result = await service.forecast(location.lat, location.lon)
        except RuntimeError:
            return Response(ResponseKind.ERROR, [Line("FC unavailable · no safe cached forecast.")])
        imperial = config.node.units == "imperial"

        def temperature(value: object) -> str:
            if value is None:
                return "—"
            celsius = float(value)
            return str(round(celsius * 9 / 5 + 32 if imperial else celsius))

        summaries = {
            "partly cloudy": "pcldy",
            "mostly cloudy": "mcldy",
            "cloudy": "cldy",
            "overcast": "ovc",
            "showers": "shwrs",
            "thunderstorms": "tstm",
        }

        def summary(value: object) -> str:
            text = str(value).lower()
            for phrase, short in summaries.items():
                text = text.replace(phrase, short)
            return text if long else text[:18].rstrip()

        parts = []
        for day in result.daily[:days]:
            precipitation = day.get("precipitation_probability")
            precipitation_text = f"{precipitation}%" if precipitation is not None else "—"
            parts.append(
                f"{str(day['name'])[:3]} {temperature(day['high_c'])}/"
                f"{temperature(day['low_c'])} {summary(day['summary'])} "
                f"{precipitation_text}"
            )
        if not parts:
            return Response(ResponseKind.ERROR, [Line("FC unavailable.")])
        age = "now" if result.age_seconds < 60 else f"{result.age_seconds // 60}m"
        stale = " cached" if result.stale else ""
        return Response(
            ResponseKind.DETAIL,
            [Line(_fit_radio(f"{' · '.join(parts)}\n{result.provider}{stale} {age}"))],
        )

    values = [
        CommandSpec(
            "WX",
            ("WEATHER",),
            module="env",
            min_trust=TrustLevel.GUEST,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short="WX [TODAY|TOMORROW|HOURLY] · local weather",
            handler=weather,
        ),
        CommandSpec(
            "FC",
            ("FORECAST",),
            module="env",
            min_trust=TrustLevel.GUEST,
            airtime_class=TrafficClass.REPLY,
            max_parts=1,
            rate_key="commands",
            help_short="FC [1-5] [-long] · local forecast",
            handler=forecast,
        ),
    ]
    if cap_alerts is not None:

        async def warn(ctx: CommandContext) -> Response:
            alerts = [
                item
                for item in await cap_alerts.list()
                if item["decision"] == "accepted" and item["review_state"] != "dismissed"
            ]
            token = ctx.args.strip()
            if token:
                if not token.isdigit():
                    return Response(ResponseKind.ERROR, [Line("WARN needs alert number.")])
                item = next((value for value in alerts if value["id"] == int(token)), None)
                if item is None:
                    return Response(
                        ResponseKind.ERROR, [Line("No active public alert by that number.")]
                    )
                detail = str(item.get("description") or item["headline"]).replace("\n", " ")
                text = (
                    f"WARN {item['id']} · {item['event']} · {item['area_desc']} · "
                    f"{item['severity']}/{item['urgency']} · {detail[:260]}"
                )
                return Response(ResponseKind.DETAIL, [Line(_fit_radio(text))])
            if not alerts:
                return Response(ResponseKind.LISTING, [Line("No active NWS alerts here.")])
            lines = [
                Line(f"WARN {item['id']} · {item['event']} · {item['area_desc']}")
                for item in alerts[:3]
            ]
            return Response(ResponseKind.LISTING, lines)

        values.append(
            CommandSpec(
                "WARN",
                ("WARNINGS",),
                module="env",
                min_trust=TrustLevel.GUEST,
                airtime_class=TrafficClass.REPLY,
                max_parts=1,
                rate_key="commands",
                help_short="WARN [number] · active official alerts",
                handler=warn,
            )
        )
    if astronomy is not None:

        async def sun(ctx: CommandContext) -> Response:
            location = config.node.location
            if location is None:
                return Response(
                    ResponseKind.ERROR, [Line("SUN unavailable · Outpost location not set.")]
                )
            value = astronomy.current(location.lat, location.lon, config.node.timezone)

            def clock(stamp: str | None) -> str:
                return stamp[11:16] if stamp else "—"

            daylight = (
                f"{value.daylight_minutes // 60}h{value.daylight_minutes % 60:02d}"
                if value.daylight_minutes is not None
                else "—"
            )
            if value.sunrise is None and value.sunset is None:
                text = (
                    "SUN no sunrise · no sunset · "
                    f"moon {value.moon_illumination}% {value.moon_phase.lower()}"
                )
                return Response(ResponseKind.DETAIL, [Line(_fit_radio(text))])
            text = (
                f"SUN rise {clock(value.sunrise)} set {clock(value.sunset)} · "
                f"civil {clock(value.civil_dawn)}/{clock(value.civil_dusk)} · "
                f"day {daylight} · moon {value.moon_illumination}% {value.moon_phase.lower()}"
            )
            return Response(ResponseKind.DETAIL, [Line(_fit_radio(text))])

        values.append(
            CommandSpec(
                "SUN",
                ("ASTRO",),
                module="env",
                min_trust=TrustLevel.GUEST,
                airtime_class=TrafficClass.REPLY,
                max_parts=1,
                rate_key="commands",
                help_short="SUN · sunrise, twilight, and moon",
                handler=sun,
            )
        )
    if seismic is not None:

        async def quake(ctx: CommandContext) -> Response:
            items = await seismic.list()
            token = ctx.args.strip()
            if token:
                if not token.isdigit():
                    return Response(ResponseKind.ERROR, [Line("QUAKE needs event number.")])
                item = next((value for value in items if value["id"] == int(token)), None)
                if item is None:
                    return Response(
                        ResponseKind.ERROR, [Line("No recent nearby quake by that number.")]
                    )
                text = (
                    f"QUAKE {item['id']} · M{item['magnitude']:.1f} · {item['distance_km']:.0f}km "
                    f"at {item['bearing_deg']}° · depth {item['depth_km']:.1f}km · "
                    f"{item['place']} · USGS"
                )
                return Response(ResponseKind.DETAIL, [Line(_fit_radio(text))])
            if not items:
                return Response(ResponseKind.LISTING, [Line("No nearby earthquakes in 24h.")])
            return Response(
                ResponseKind.LISTING,
                [
                    Line(
                        f"QUAKE {item['id']} · M{item['magnitude']:.1f} · "
                        f"{item['distance_km']:.0f}km {item['bearing_deg']}° · {item['place']}"
                    )
                    for item in items[:2]
                ],
            )

        values.append(
            CommandSpec(
                "QUAKE",
                ("EARTHQUAKE",),
                module="env",
                min_trust=TrustLevel.GUEST,
                airtime_class=TrafficClass.REPLY,
                max_parts=1,
                rate_key="commands",
                help_short="QUAKE [number] · nearby USGS earthquakes",
                handler=quake,
            )
        )
    if waypoints is not None:

        async def position(ctx: CommandContext) -> Response:
            handle = ctx.args.strip().lower().removeprefix("@")
            share = handle.split()
            if share and share[0] == "share":
                if len(share) == 1:
                    preference = await waypoints.position_privacy(ctx.member.id)
                    return Response(
                        ResponseKind.DETAIL,
                        [Line(f"Position sharing: {preference} · POS SHARE full|coarse|off")],
                    )
                if len(share) != 2:
                    return Response(
                        ResponseKind.ERROR, [Line("Use POS SHARE full, coarse, or off.")]
                    )
                try:
                    preference = await waypoints.set_position_privacy(ctx.member.id, share[1])
                except ValueError as error:
                    return Response(ResponseKind.ERROR, [Line(str(error))])
                descriptions = {
                    "full": "members may query precise position",
                    "coarse": "member queries rounded to local area",
                    "off": "position cannot be queried",
                }
                return Response(
                    ResponseKind.ACK,
                    [Line(f"✓ Position sharing {preference} · {descriptions[preference]}")],
                )
            target = await waypoints.member_position(
                handle=handle if handle else None,
                member_id=None if handle else ctx.member.id,
            )
            if target is None:
                return Response(ResponseKind.ERROR, [Line("No shared position found.")])
            coordinates = waypoints.privacy_position(
                target,
                config.security.coarse_precision_m,
                ctx.member.trust == "operator",
            )
            if coordinates is None:
                return Response(ResponseKind.ERROR, [Line("Position sharing is off.")])
            latitude, longitude = coordinates
            origin = await waypoints.member_position(member_id=ctx.member.id)
            origin_coordinates = (
                waypoints.privacy_position(origin, config.security.coarse_precision_m, True)
                if origin
                else None
            )
            if origin_coordinates is not None and origin["id"] != target["id"]:
                km, bearing = waypoints.distance_bearing(
                    origin_coordinates[0],
                    origin_coordinates[1],
                    {"latitude": latitude, "longitude": longitude},
                )
                distance_value = km / 1.609344 if config.node.units == "imperial" else km
                range_text = (
                    f" · {distance_value:.1f}{'mi' if config.node.units == 'imperial' else 'km'} "
                    f"at {bearing}°"
                )
            else:
                range_text = ""
            label = f"@{target['handle']}" if target["handle"] else target["mesh_id"]
            map_url = f"https://maps.google.com/?q={latitude:.5f},{longitude:.5f}"
            return Response(
                ResponseKind.DETAIL,
                [Line(f"{label} · {latitude:.5f},{longitude:.5f}{range_text} · {map_url}")],
            )

        async def waypoint(ctx: CommandContext) -> Response:
            token = ctx.args.strip()
            if token.lower().startswith("add "):
                if TrustLevel.parse(ctx.member.trust) < TrustLevel.MEMBER:
                    return Response(ResponseKind.ERROR, [Line("Members only.")])
                name = token[4:].strip()
                if not name:
                    return Response(ResponseKind.ERROR, [Line("WP ADD needs a name.")])
                origin = await waypoints.member_position(member_id=ctx.member.id)
                if origin is None:
                    return Response(
                        ResponseKind.ERROR,
                        [Line("No GPS position received. Share position, then retry WP ADD.")],
                    )
                try:
                    item = await waypoints.create(
                        name, float(origin["lat"]), float(origin["lon"]), "general", ""
                    )
                except ValueError as error:
                    return Response(ResponseKind.ERROR, [Line(str(error))])
                return Response(
                    ResponseKind.ACK,
                    [Line(f"✓ Public waypoint {item['name']} saved at current position.")],
                )
            if not token:
                items = await waypoints.list()
                if not items:
                    return Response(ResponseKind.LISTING, [Line("No saved waypoints.")])
                return Response(
                    ResponseKind.LISTING,
                    [Line(f"WP {item['slug']} · {item['name']}") for item in items[:5]],
                )
            try:
                item = await waypoints.by_token(token)
            except ValueError:
                item = None
            if item is None:
                return Response(ResponseKind.ERROR, [Line("Waypoint not found.")])
            latitude = float(item["latitude"])
            longitude = float(item["longitude"])
            map_url = f"https://maps.google.com/?q={latitude:.5f},{longitude:.5f}"
            return Response(
                ResponseKind.DETAIL,
                [
                    Line(
                        f"{item['name']} · {latitude:.5f},{longitude:.5f} · "
                        f"{item['category']} · {map_url}"
                    )
                ],
            )

        async def waypoint_list(ctx: CommandContext) -> Response:
            argument = ctx.args.strip()
            if argument and not argument.replace(".", "", 1).isdigit():
                return Response(ResponseKind.ERROR, [Line("WPS radius must be a number.")])
            radius = float(argument) if argument else None
            if radius is not None and not 0 < radius <= 500:
                return Response(ResponseKind.ERROR, [Line("WPS radius must be 0-500 km.")])
            origin = await waypoints.member_position(member_id=ctx.member.id)
            if origin is not None:
                origin_lat, origin_lon = float(origin["lat"]), float(origin["lon"])
            elif config.node.location is not None:
                origin_lat, origin_lon = config.node.location.lat, config.node.location.lon
            else:
                origin_lat = origin_lon = None
            items = await waypoints.list()
            located = []
            for item in items:
                if origin_lat is None or origin_lon is None:
                    located.append((None, item))
                    continue
                km, _ = waypoints.distance_bearing(origin_lat, origin_lon, item)
                if radius is None or km <= radius:
                    located.append((km, item))
            located.sort(key=lambda value: value[0] if value[0] is not None else 0)
            if not located:
                return Response(ResponseKind.LISTING, [Line("No waypoints in range.")])
            imperial = config.node.units == "imperial"
            lines = []
            for km, item in located[:5]:
                distance = ""
                if km is not None:
                    amount = km / 1.609344 if imperial else km
                    distance = f" · {amount:.1f}{'mi' if imperial else 'km'}"
                lines.append(Line(f"WP {item['slug']} · {item['name']}{distance}"))
            return Response(ResponseKind.LISTING, lines)

        async def distance(ctx: CommandContext) -> Response:
            location = config.node.location
            if location is None:
                return Response(
                    ResponseKind.ERROR, [Line("DIST unavailable · Outpost location not set.")]
                )
            try:
                item = await waypoints.by_token(ctx.args.strip())
            except ValueError:
                item = None
            if item is None:
                return Response(ResponseKind.ERROR, [Line("DIST needs a saved waypoint.")])
            km, bearing = waypoints.distance_bearing(location.lat, location.lon, item)
            distance_value = km / 1.609344 if config.node.units == "imperial" else km
            unit = "mi" if config.node.units == "imperial" else "km"
            return Response(
                ResponseKind.DETAIL,
                [Line(f"{item['name']} · {distance_value:.1f}{unit} at {bearing}° from Outpost")],
            )

        values.extend(
            [
                CommandSpec(
                    "POS",
                    (),
                    module="env",
                    min_trust=TrustLevel.MEMBER,
                    airtime_class=TrafficClass.REPLY,
                    max_parts=1,
                    rate_key="positions",
                    help_short="POS [handle] / POS SHARE full|coarse|off",
                    handler=position,
                ),
                CommandSpec(
                    "WAYPOINT",
                    ("WP",),
                    module="env",
                    min_trust=TrustLevel.GUEST,
                    airtime_class=TrafficClass.REPLY,
                    max_parts=1,
                    rate_key="commands",
                    help_short="WP [name] / WP ADD <name> · public waypoint",
                    handler=waypoint,
                ),
                CommandSpec(
                    "WPS",
                    (),
                    module="env",
                    min_trust=TrustLevel.GUEST,
                    airtime_class=TrafficClass.REPLY,
                    max_parts=1,
                    rate_key="commands",
                    help_short="WPS [radius_km] · nearby public waypoints",
                    handler=waypoint_list,
                ),
                CommandSpec(
                    "DIST",
                    ("DISTANCE",),
                    module="env",
                    min_trust=TrustLevel.GUEST,
                    airtime_class=TrafficClass.REPLY,
                    max_parts=1,
                    rate_key="commands",
                    help_short="DIST <waypoint> · range and bearing",
                    handler=distance,
                ),
            ]
        )
    return values
