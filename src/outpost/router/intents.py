from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml
from prometheus_client import Counter, Gauge

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
INTENT_LOAD_ATTEMPTS = Counter(
    "outpost_router_intent_load_attempts_total",
    "Configured tolerant-intent map load attempts",
    ("outcome",),
)
INTENT_CONFIGURED_ENTRIES = Gauge(
    "outpost_router_intent_configured_entries",
    "Configured tolerant-intent entries in the most recent load",
    ("result",),
)
LOGGER = logging.getLogger(__name__)

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
        self._configured_loaded = 0
        self._configured_rejected = 0
        self._load_error: str | None = None
        self._exists = False
        self._state = "unloaded"
        self._issues: tuple[dict[str, object], ...] = ()
        self._last_warnings: frozenset[tuple[int | None, str]] = frozenset()

    def _warn(self, issues: list[dict[str, object]]) -> None:
        current: frozenset[tuple[int | None, str]] = frozenset(
            (
                index if isinstance(index := issue.get("index"), int) else None,
                str(issue["reason"]),
            )
            for issue in issues
        )
        for index, reason in sorted(
            current - self._last_warnings,
            key=lambda value: (-1 if value[0] is None else value[0], value[1]),
        ):
            entry = "file" if index is None else f"entry {index}"
            LOGGER.warning("Intent map %s %s rejected: %s", self.path, entry, reason)
        self._last_warnings = current

    def _reload(self) -> None:
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            mtime_ns = -1
        if self._patterns and self._load_error is None and mtime_ns == self._mtime_ns:
            return
        configured: list[tuple[int, str, str]] = []
        rejected = 0
        error: str | None = None
        issues: list[dict[str, object]] = []
        self._exists = mtime_ns >= 0
        if mtime_ns >= 0:
            try:
                payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or []
                if not isinstance(payload, list):
                    raise TypeError("intent map must contain a list")
                for index, item in enumerate(payload, start=1):
                    pattern = item.get("pattern") if isinstance(item, dict) else None
                    command = item.get("command") if isinstance(item, dict) else None
                    if (
                        isinstance(pattern, str)
                        and pattern.strip()
                        and isinstance(command, str)
                        and command.strip()
                    ):
                        configured.append((index, pattern, command))
                    else:
                        rejected += 1
                        reason = (
                            "entry must be a mapping"
                            if not isinstance(item, dict)
                            else "pattern and command must be non-empty strings"
                        )
                        issues.append({"index": index, "reason": reason})
            except OSError as exc:
                configured = []
                reason = exc.strerror or "I/O error"
                error = f"OSError: configured intent map could not be read ({reason})"
                issues.append({"index": None, "reason": error})
            except TypeError as exc:
                configured = []
                error = f"TypeError: {exc}"
                issues.append({"index": None, "reason": error})
            except yaml.YAMLError as exc:
                configured = []
                mark = getattr(exc, "problem_mark", None)
                location = f" at line {mark.line + 1}" if mark is not None else ""
                error = f"{type(exc).__name__}: configured intent map could not be parsed{location}"
                issues.append({"index": None, "reason": error})
            except (UnicodeError, ValueError) as exc:
                configured = []
                error = f"{type(exc).__name__}: configured intent map could not be parsed"
                issues.append({"index": None, "reason": error})
        else:
            error = "configured intent map was not found"
            issues.append({"index": None, "reason": error})
        patterns: list[tuple[re.Pattern[str], str]] = []
        loaded = 0
        for index, pattern, command in configured:
            try:
                patterns.append((re.compile(pattern, re.IGNORECASE), command))
            except re.error as exc:
                rejected += 1
                issues.append({"index": index, "reason": f"invalid regex: {exc.msg}"})
                continue
            loaded += 1
        for pattern, command in BUILTIN_INTENTS:
            try:
                patterns.append((re.compile(pattern, re.IGNORECASE), command))
            except re.error:
                continue
        if not self._exists:
            state = "missing"
        elif error is not None:
            state = "error"
        elif loaded and rejected:
            state = "partial"
        elif rejected:
            state = "rejected_all"
        elif loaded:
            state = "ready"
        else:
            state = "empty"
        issues.sort(key=lambda item: index if isinstance(index := item.get("index"), int) else -1)
        self._patterns = tuple(patterns)
        self._mtime_ns = mtime_ns if error is None else None
        self._configured_loaded = loaded
        self._configured_rejected = rejected
        self._load_error = error
        self._state = state
        self._issues = tuple(issues)
        self._warn(issues)
        INTENT_LOAD_ATTEMPTS.labels(state).inc()
        INTENT_CONFIGURED_ENTRIES.labels("loaded").set(loaded)
        INTENT_CONFIGURED_ENTRIES.labels("rejected").set(rejected)

    def status(self) -> dict[str, object]:
        """Return content-safe parse evidence for readiness diagnostics."""
        self._reload()
        return {
            "path": str(self.path),
            "exists": self._exists,
            "loaded": self._configured_loaded,
            "rejected": self._configured_rejected,
            "builtin": len(BUILTIN_INTENTS),
            "state": self._state,
            "error": self._load_error,
            "issues": list(self._issues),
        }

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
