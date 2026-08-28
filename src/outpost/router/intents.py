from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml
from prometheus_client import Counter

from .models import TrustLevel
from .registry import CommandRegistry

TOLERANT_MATCHES = Counter(
    "outpost_router_tolerant_matches_total",
    "Command inputs recovered without exact command syntax",
    ("mode", "command"),
)
TOLERANT_REJECTIONS = Counter(
    "outpost_router_tolerant_rejections_total",
    "Command inputs not automatically corrected for safety",
    ("reason", "command"),
)

BUILTIN_INTENTS = (
    (r"^(?:help|menu|commands|what can you do)\??$", "MENU"),
    (r"^(?:what(?:'s| is) the weather|weather now|current weather)\??$", "WX"),
    (r"^(?:forecast|weather forecast|is it going to rain)\??$", "FC 5"),
    (r"^(?:alerts|warnings|any alerts)\??$", "WARN"),
    (r"^(?:incidents|active incidents|what(?:'s| is) happening)\??$", "INCIDENTS"),
    (r"^(?:boards|community|community boards)\??$", "BOARDS"),
    (r"^(?:mail|inbox|my messages)\??$", "MAIL"),
    (r"^(?:send mail|send a message|new message)$", "SEND"),
    (r"^(?:who(?:'s| is) (?:around|here|online)|people nearby)\??$", "WHO"),
    (r"^(?:anything new|what(?:'s| is) new|catch me up)\??$", "NEW"),
    (r"^(?:i(?:'m| am) ok|checking in|all clear)$", "OK"),
    (r"^(?:i need help|need help)$", "HELPME"),
    (r"^(?:check in|check-in)$", "MENU CHECKIN"),
    (r"^(?:report a problem|report incident|new incident)$", "MENU REPORT"),
    (r"^(?:where am i|my (?:position|location))\??$", "POS"),
)


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", value)


def _distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class IntentResolution:
    invoked: str
    mode: str | None = None
    candidates: tuple[str, ...] = ()


class IntentResolver:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._mtime_ns: int | None = None
        self._patterns: tuple[tuple[re.Pattern[str], str], ...] = ()

    def _reload(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = -1
        if self._patterns and mtime_ns == self._mtime_ns:
            return
        configured: list[tuple[str, str]] = []
        if mtime_ns >= 0:
            try:
                payload = yaml.safe_load(self.path.read_text()) or []
                configured = [
                    (str(item["pattern"]), str(item["command"]))
                    for item in payload
                    if isinstance(item, dict) and "pattern" in item and "command" in item
                ]
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                configured = []
        patterns: list[tuple[re.Pattern[str], str]] = []
        for pattern, command in (*configured, *BUILTIN_INTENTS):
            try:
                patterns.append((re.compile(pattern, re.IGNORECASE), command))
            except re.error:
                continue
        self._patterns = tuple(patterns)
        self._mtime_ns = mtime_ns

    @staticmethod
    def _allowed(command: str, trust: str, registry: CommandRegistry) -> bool:
        spec = registry.resolve(command.split(maxsplit=1)[0])
        return bool(spec and TrustLevel.parse(trust) >= spec.min_trust)

    def resolve(
        self,
        invoked: str,
        trust: str,
        registry: CommandRegistry,
    ) -> IntentResolution:
        token, separator, args = invoked.strip().partition(" ")
        normalized_token = _normalized(token)
        if len(normalized_token) >= 3:
            trust_level = TrustLevel.parse(trust)
            max_distance = 1 if len(normalized_token) <= 4 else 2
            mutation_matches: list[tuple[int, str]] = []
            for spec in registry.known_commands():
                if not spec.mutates:
                    continue
                for name in (spec.name, *spec.aliases):
                    normalized_name = _normalized(name)
                    if len(normalized_name) < 3:
                        continue
                    distance = _distance(normalized_token, normalized_name)
                    if distance <= max_distance:
                        mutation_matches.append((distance, spec.name))
            if mutation_matches:
                best_distance = min(value[0] for value in mutation_matches)
                best_mutations = sorted(
                    {value[1] for value in mutation_matches if value[0] == best_distance}
                )
                eligible: list[str] = []
                for command in best_mutations:
                    candidate_spec = registry.resolve(command)
                    if candidate_spec is not None and trust_level >= candidate_spec.min_trust:
                        eligible.append(command)
                candidates = tuple(eligible)
                label = candidates[0].lower() if len(candidates) == 1 else "multiple"
                mode = "mutation_confirmation" if candidates else "mutation_protected"
                TOLERANT_REJECTIONS.labels(mode, label).inc()
                return IntentResolution(invoked, mode, candidates[:3])

            matches: list[tuple[int, str, str]] = []
            for spec in registry.commands():
                if spec.mutates or trust_level < spec.min_trust:
                    continue
                for name in (spec.name, *spec.aliases):
                    normalized_name = _normalized(name)
                    if len(normalized_name) < 3:
                        continue
                    distance = _distance(normalized_token, normalized_name)
                    if distance <= max_distance:
                        matches.append((distance, spec.name, name))
            if matches:
                best_distance = min(value[0] for value in matches)
                best_specs = sorted({value[1] for value in matches if value[0] == best_distance})
                if len(best_specs) == 1:
                    command = best_specs[0]
                    TOLERANT_MATCHES.labels("fuzzy", command.lower()).inc()
                    return IntentResolution(f"{command}{' ' + args if separator else ''}", "fuzzy")
                TOLERANT_REJECTIONS.labels("ambiguous", "multiple").inc()
                return IntentResolution(invoked, "ambiguous", tuple(best_specs[:3]))
        self._reload()
        normalized_invoked = _normalized(invoked)
        for pattern, command in self._patterns:
            if pattern.search(normalized_invoked) and self._allowed(command, trust, registry):
                target = command.split(maxsplit=1)[0]
                TOLERANT_MATCHES.labels("intent", target.lower()).inc()
                return IntentResolution(command, "intent")
        return IntentResolution(invoked)
