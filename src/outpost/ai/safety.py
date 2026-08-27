from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from outpost.ai.budget import EvidenceChunk

_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_CITATION = re.compile(r"\bsrc:\s*([^\s,;]+)", re.IGNORECASE)
_INJECTION = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior)|hidden\s+(?:system\s+)?prompt|"
    r"developer\s+message|disable\s+safety|treat\s+evidence\s+as\s+instructions|"
    r"reveal\s+(?:all\s+)?private|answer\s+without\s+(?:the\s+)?ai\s+marker|"
    r"reply\s+only\s+|use\s+src\s+fake)",
    re.IGNORECASE,
)
_SYSTEM_LEAK = re.compile(
    r"(?:EVIDENCE \(UNTRUSTED DATA|You are .{0,80}assistant for a local community radio|"
    r"No greetings, no sign-offs|system prompt)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Refusal:
    reason: str
    text: str


@dataclass(frozen=True)
class FilteredOutput:
    accepted: bool
    text: str | None
    reason: str | None = None


def fit_bytes(text: str, limit: int = 200) -> str:
    if len(text.encode()) <= limit:
        return text
    suffix = "…"
    encoded = text.encode()[: max(0, limit - len(suffix.encode()))]
    bounded = encoded.decode(errors="ignore").rstrip()
    while bounded and len((bounded + suffix).encode()) > limit:
        bounded = bounded[:-1].rstrip()
    return bounded + suffix


def prefilter(question: str, emergency_number: str = "911") -> Refusal | None:
    normalized = " ".join(question.casefold().split())
    if _INJECTION.search(normalized):
        return Refusal("prompt_injection", "[AI] I can't follow prompt-changing instructions.")
    if re.search(r"\b(?:hurt|kill) myself\b|\bsuicid|\bself[- ]harm\b", normalized):
        return Refusal(
            "self_harm",
            fit_bytes(
                f"[AI] I can't help with self-harm instructions. Call/text 988 or "
                f"{emergency_number} now; tell a trusted person nearby."
            ),
        )
    if re.search(
        r"\b(?:dose|dosage|how much|how many)\b.{0,80}\b(?:ibuprofen|acetaminophen|"
        r"medicine|medication|pill|drug)\b",
        normalized,
    ):
        return Refusal(
            "medical_dosing",
            fit_bytes(
                f"[AI] I can't give medical dosing. Ask a clinician or poison control; "
                f"call {emergency_number} for an emergency."
            ),
        )
    if re.search(r"\bdiagnos|what(?:'s| is) wrong with me|\bchest pain\b", normalized):
        return Refusal(
            "diagnosis",
            fit_bytes(
                f"[AI] I can't diagnose symptoms. Contact a clinician; call "
                f"{emergency_number} for urgent symptoms."
            ),
        )
    if re.search(
        r"\b(?:stop|start|change)\b.{0,40}\b(?:prescription|medication|treatment)\b",
        normalized,
    ):
        return Refusal(
            "treatment",
            "[AI] I can't make treatment decisions. Contact the prescribing clinician.",
        )
    if re.search(r"\blegal advice\b|\bshould i sue\b|\bmy lawyer\b", normalized):
        return Refusal(
            "legal", "[AI] I can't give legal advice. Contact a qualified local adviser."
        )
    if re.search(
        r"\b(?:raise|send|broadcast|trigger|sound|write)\b.{0,50}\b(?:alert|alarm)\b",
        normalized,
    ):
        return Refusal(
            "alarm_action",
            "[AI] I can't raise alerts. Use REPORT to notify the operator and local responders.",
        )
    if re.search(r"\b(?:contact|call|notify)\b.{0,30}\b(?:police|authorit|sheriff)\b", normalized):
        return Refusal(
            "authority_contact",
            fit_bytes(
                f"[AI] I can't contact authorities. Call {emergency_number} yourself; "
                "use REPORT to notify the operator."
            ),
        )
    if re.search(r"\b(?:private\s+)?(?:mail|inbox|messages)\b", normalized) and re.search(
        r"\b(?:show|read|reveal|tell|dana|ray|morgan|their|other)\b", normalized
    ):
        return Refusal("private_mail", "[AI] I can't access or reveal private mail.")
    if re.search(r"\b(?:private\s+)?(?:operator\s+)?notes?\b", normalized):
        return Refusal("private_notes", "[AI] I can't access or reveal operator notes.")
    if re.search(
        r"\bwhere is\s+@?[a-z0-9_-]+\s+(?:right now|now|currently)\b|"
        r"\b(?:show|reveal)\b.{0,30}\b(?:position|location)\b",
        normalized,
    ):
        return Refusal("private_position", "[AI] I can't access or reveal member positions.")
    return None


def unsafe_evidence(chunks: Sequence[EvidenceChunk]) -> bool:
    return any(_INJECTION.search(chunk.text) for chunk in chunks)


def postfilter(
    answer: str,
    *,
    evidence_refs: Sequence[str],
    grounded: bool,
) -> FilteredOutput:
    text = " ".join(answer.split()).strip()
    if not text:
        return FilteredOutput(False, None, "empty")
    if _URL.search(text):
        return FilteredOutput(False, None, "url")
    if _SYSTEM_LEAK.search(text):
        return FilteredOutput(False, None, "system_prompt_leak")
    expected_marker = "[AI]" if grounded else "[AI?]"
    if not text.startswith(expected_marker):
        return FilteredOutput(False, None, "marker")
    citations = _CITATION.findall(text)
    if grounded:
        if not citations:
            return FilteredOutput(False, None, "missing_citation")
        allowed = set(evidence_refs)
        if any(value.rstrip(".])") not in allowed for value in citations):
            return FilteredOutput(False, None, "invented_citation")
    elif citations:
        return FilteredOutput(False, None, "ungrounded_citation")
    if len(text.encode()) > 200:
        return FilteredOutput(False, None, "too_long")
    return FilteredOutput(True, text)


def extractive_fallback(chunks: Sequence[EvidenceChunk]) -> str:
    """Return a bounded, evidence-only answer when model formatting cannot be trusted."""
    if not chunks or unsafe_evidence(chunks):
        return "[AI] No safe local answer. Try BOARDS or ask the operator."
    chunk = chunks[0]
    body = _URL.sub("", " ".join(chunk.text.split())).strip()
    suffix = f" src: {chunk.ref}"
    prefix = "[AI] "
    budget = 200 - len((prefix + suffix).encode())
    body = fit_bytes(body, max(20, budget)).removesuffix("…").rstrip()
    return fit_bytes(f"{prefix}{body}{suffix}")
