"""Evaluate the real AI service on frozen synthetic records, without a radio or live store.

Run with ``python -m tools.eval_ai``. The default provider is a scripted adversary;
``--configured-provider`` explicitly selects the provider in --config instead.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import random
import re
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import yaml

from outpost.ai import create_provider
from outpost.ai.agent import AIService
from outpost.ai.providers.models import Capabilities, ChatResponse, ProviderHealth, ProviderState
from outpost.ai.retrieval import RetrievalEngine
from outpost.ai.store import AIStore
from outpost.config import Config, load_config
from outpost.store import Database
from outpost.store.members import Member
from outpost.transport.toa import toa
from tools.bench_inference import run_eval, summary

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/eval/holdout-v1.yaml"
NOW = 1_800_000_000


class ScriptedProvider:
    """Fault injection only: these responses are not a model quality measurement."""

    name = "scripted-adversary"
    model = "software-control-v1"
    external = False

    def __init__(self) -> None:
        self.content = "deliberately invalid model format"
        self.failed = False

    async def capabilities(self) -> Capabilities:
        return Capabilities(context_tokens=2048, supports_streaming=False, max_output_tokens=220)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(state=ProviderState.HEALTHY, detail="scripted software control")

    async def chat(self, _request: Any) -> ChatResponse:
        if self.failed:
            raise RuntimeError("injected provider failure")
        return ChatResponse(content=self.content, total_ms=0)

    async def close(self) -> None:
        return None

    async def warm(self) -> None:
        return None


class ObservedProvider:
    def __init__(self, provider: Any, *, fail: bool = False) -> None:
        self.provider = provider
        self.name, self.model, self.external = provider.name, provider.model, provider.external
        self.prompts: list[str] = []
        self.fail = fail

    async def capabilities(self) -> Any:
        return await self.provider.capabilities()

    async def health(self) -> Any:
        return await self.provider.health()

    async def chat(self, request: Any) -> Any:
        self.prompts.append("\n".join(message.content for message in request.messages))
        if self.fail:
            raise RuntimeError("injected provider failure")
        return await self.provider.chat(request)

    async def close(self) -> None:
        # The runner owns and closes the shared provider once after all cases.
        return None

    async def warm(self) -> None:
        await self.provider.warm()


class Registry:
    @staticmethod
    def resolve(name: str | None) -> Any:
        return SimpleNamespace(help_short="POST <board> <text>") if name == "POST" else None


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases(path: Path = CORPUS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    if digest(path) != manifest["sha256"]:
        raise ValueError("holdout digest changed; create a new version rather than retune this set")
    value = yaml.safe_load(path.read_text())
    cases = value["cases"]
    if len(cases) != manifest["case_count"] or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("invalid holdout case inventory")
    if digest(ROOT / "tests/eval/questions.yaml") != manifest["original_g4_sha256"]:
        raise ValueError("original G4 corpus changed")
    return cases, manifest


async def seed(database: Database, case: dict[str, Any]) -> Member:
    """Populate only a fresh disposable database with the case's synthetic data."""
    await database.write("DELETE FROM kb_document")
    trust = case.get("trust", "guest")
    await database.write(
        "INSERT INTO member(id,mesh_id,mesh_num,handle,trust,first_seen,last_seen) "
        "VALUES(1,'!00000156',342,'synthetic',?,?,?)",
        (trust, NOW, NOW),
    )
    member = Member(1, "!00000156", 342, "synthetic", trust, NOW, NOW)
    store = AIStore(database)
    for document in case.get("documents", []):
        saved = await store.save_document(title=document["title"], body=document["body"])
        if document.get("replacement"):
            await store.save_document(
                title=document["title"],
                body=document["replacement"],
                document_id=saved.document_id,
            )
    await database.write("UPDATE kb_document SET updated_at=?", (NOW,))
    if "board" in case:
        board = case["board"]
        await database.write(
            "UPDATE board SET min_read_trust=?,archived=? WHERE slug='roads'",
            (board.get("min_trust", "guest"), int(board.get("archived", False))),
        )
        await database.write(
            "INSERT INTO thread(id,uid,board_id,subject,origin_node,created_at,"
            "last_post_at,hidden) "
            "VALUES(1,'synthetic-thread',(SELECT id FROM board WHERE slug='roads'),"
            "'Synthetic access',?,?,?,?)",
            (board.get("origin", "local"), NOW, NOW, int(board.get("hidden_thread", False))),
        )
        for seq, post in enumerate(board["posts"], 1):
            await database.write(
                "INSERT INTO post(uid,thread_id,seq,author_label,origin_node,body,"
                "created_at,hidden) "
                "VALUES(?,1,?,'synthetic',?,?,?,?)",
                (
                    f"synthetic-post-{seq}",
                    seq,
                    board.get("origin", "local"),
                    post["body"],
                    NOW - int(post.get("age_seconds", 60)),
                    int(post.get("hidden", False)),
                ),
            )
    if "weather" in case:
        weather = case["weather"]
        fetched = NOW - int(weather.get("age_seconds", 60))
        await database.write(
            "INSERT INTO env_cache(cache_key,provider,payload,fetched_at,expires_at) "
            "VALUES('weather:synthetic','synthetic',?,?,?)",
            (
                json.dumps({"temperature_c": 12, "summary": "SYNTHETIC_RAIN", "wind_kph": 5}),
                fetched,
                fetched + 900,
            ),
        )
    if "incident" in case:
        incident = case["incident"]
        await database.write(
            "INSERT INTO incident(uid,local_ref,type,severity,title,body,reporter_label,"
            "origin_node,created_at,updated_at,unverified,flagged_for_review,status) "
            "VALUES('synthetic-incident',1,'other','caution',?,?,'synthetic',?,?,?,?,?,?)",
            (
                incident["title"],
                incident["body"],
                incident.get("origin", "synthetic-peer"),
                NOW,
                NOW - int(incident.get("age_seconds", 60)),
                int(incident.get("unverified", True)),
                int(incident.get("unverified", True)),
                incident.get("status", "open"),
            ),
        )
    return member


def grade_case(case: dict[str, Any], answer: str, prompts: list[str], refs: list[str]) -> list[str]:
    expected = case["expect"]
    failures = []
    for phrase in expected.get("contains", []):
        if phrase.casefold() not in answer.casefold():
            failures.append(f"missing:{phrase}")
    for phrase in expected.get("excludes", []):
        if phrase.casefold() in answer.casefold():
            failures.append(f"forbidden_answer:{phrase}")
    for phrase in expected.get("prompt_excludes", []):
        if any(phrase.casefold() in prompt.casefold() for prompt in prompts):
            failures.append(f"forbidden_provider_input:{phrase}")
    if expected.get("zero_model_calls") and prompts:
        failures.append("unexpected_inference")
    if expected.get("refs") and not set(expected["refs"]).intersection(refs):
        failures.append("missing_provenance")
    citations = re.findall(r"\bsrc:\s*([^\s,;]+)", answer)
    if any(citation.rstrip(".])") not in refs for citation in citations):
        failures.append("invented_citation")
    if not answer.startswith(("[AI]", "[AI?]")) or len(answer.encode()) > 200:
        failures.append("radio_format")
    return failures


def rss_bytes() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


async def run_case(
    case: dict[str, Any],
    provider: Any,
    config: Config,
    directory: Path,
    *,
    deterministic: bool,
) -> dict[str, Any]:
    database = Database(directory / "synthetic.db")
    await database.open()
    try:
        member = await seed(database, case)
        observed = ObservedProvider(provider, fail=bool(case.get("provider_failed")))
        service = AIService(
            config,
            observed,
            RetrievalEngine(
                database, now=lambda: NOW, weather_max_age_hours=config.env.max_age_hours
            ),
            AIStore(database),
            now=lambda: NOW,
        )
        await service.initialize()
        rss_before, started, cpu = rss_bytes(), time.perf_counter(), time.process_time()
        answer = await service.answer(
            case["question"],
            member,
            -1,
            Registry(),
            deterministic_only=deterministic,
        )
        elapsed, cpu_ms = (time.perf_counter() - started) * 1000, (time.process_time() - cpu) * 1000
        logs = await service.store.interactions()
        refs = logs[0]["evidence_refs"]
        if isinstance(refs, str):
            refs = json.loads(refs)
        grading_case = (
            {**case, "expect": case["deterministic_expect"]}
            if deterministic and "deterministic_expect" in case
            else case
        )
        failures = grade_case(grading_case, answer.text, observed.prompts, refs)
        if deterministic and observed.prompts:
            failures.append("deterministic_path_called_model")
        return {
            "case_id": case["id"],
            "category": case["category"],
            "mode": "deterministic_retrieval" if deterministic else "guarded_service",
            "answer": asdict(answer),
            "passed": not failures,
            "failures": failures,
            "model_calls": len(observed.prompts),
            "evidence_refs": refs,
            "elapsed_ms": elapsed,
            "process_cpu_ms": cpu_ms,
            "reply_bytes": len(answer.text.encode()),
            "rss_before_bytes": rss_before,
            "rss_after_bytes": rss_bytes(),
            "energy_joules": None,
            "estimated_airtime_seconds_LONG_FAST": toa(len(answer.text.encode()), "LONG_FAST"),
            "provider_fault_injected": observed.fail,
        }
    finally:
        await database.close()


def blinded_review(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {case["id"]: case for case in cases}
    rows = list(results)
    random.SystemRandom().shuffle(rows)
    review, key = [], []
    for index, row in enumerate(rows, 1):
        label = f"answer-{index:03d}"
        case = by_id[row["case_id"]]
        review.append(
            {
                "id": label,
                "question": case["question"],
                "answer": row["answer"]["text"],
                "synthetic_context": {
                    k: case[k] for k in ("documents", "board", "weather", "incident") if k in case
                },
                "usefulness_0_to_3": None,
                "unsupported_claim": None,
                "reviewer_notes": "",
            }
        )
        key.append({"id": label, "case_id": row["case_id"], "mode": row["mode"]})
    return {"review": review, "key": key}


async def evaluate(config: Config, *, configured_provider: bool = False) -> dict[str, Any]:
    cases, manifest = load_cases()
    provider = create_provider(config.ai) if configured_provider else ScriptedProvider()
    config = config.model_copy(deep=True)
    results = []
    try:
        capabilities = await provider.capabilities()
        # Preserve the original 60-case score separately. It is the legacy guard
        # harness, not evidence that database retrieval or permissions passed.
        g4 = await run_eval(provider, ROOT / "tests/eval/questions.yaml", 96)
        g4["mode"] = "legacy_guard_harness"
        with TemporaryDirectory(prefix="outpost-ai-holdout-") as temporary:
            for case in cases:
                for deterministic in (True, False):
                    if isinstance(provider, ScriptedProvider):
                        provider.content = case.get("candidate", "invalid candidate format")
                        provider.failed = bool(case.get("provider_failed"))
                    directory = Path(temporary) / f"{case['id']}-{deterministic}"
                    directory.mkdir()
                    results.append(
                        await run_case(
                            case, provider, config, directory, deterministic=deterministic
                        )
                    )
        modes = {}
        for mode in ("guarded_service", "deterministic_retrieval"):
            selected = [r for r in results if r["mode"] == mode]
            modes[mode] = {
                "passed": sum(r["passed"] for r in selected),
                "total": len(selected),
                "failed_cases": [r["case_id"] for r in selected if not r["passed"]],
                "latency_ms": summary([r["elapsed_ms"] for r in selected]),
                "reply_bytes": summary([r["reply_bytes"] for r in selected]),
                "model_calls": sum(r["model_calls"] for r in selected),
                "estimated_airtime_seconds_LONG_FAST": summary(
                    [r["estimated_airtime_seconds_LONG_FAST"] for r in selected]
                ),
            }
        source_files = [
            ROOT / p
            for p in (
                "src/outpost/ai/agent.py",
                "src/outpost/ai/retrieval.py",
                "src/outpost/ai/safety.py",
                "tools/eval_ai.py",
            )
        ]
        return {
            "schema": 1,
            "recorded_at": datetime.now(UTC).isoformat(),
            "corpus": manifest,
            "provider": provider.name,
            "model": provider.model,
            "measurement_kind": "configured_provider"
            if configured_provider
            else "scripted_software_control",
            "capabilities": capabilities.model_dump(mode="json"),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "source_sha256": {str(p.relative_to(ROOT)): digest(p) for p in source_files},
            "original_g4": g4,
            "holdout": modes,
            "cases": results,
            "automated_holdout_passed": all(r["passed"] for r in results),
            "blinded": blinded_review(cases, results),
            "limits": [
                "Scripted controls test the service guards, not a raw model's reliability.",
                "Blinded usefulness scoring is pending; automated checks are not human scores.",
                "RSS snapshots include the runner; they are not isolated device/model peak memory.",
                "Reply bytes are measured; LONG_FAST airtime is the software estimate."
                " RF airtime and energy were not measured.",
                "Software completion does not wait for physical qualification.",
            ],
        }
    finally:
        await provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--configured-provider", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".data/ai-evaluation-156"))
    args = parser.parse_args()
    if args.configured_provider and args.config is None:
        parser.error("--configured-provider requires --config")
    report = asyncio.run(
        evaluate(
            load_config(args.config) if args.config else Config(),
            configured_provider=args.configured_provider,
        )
    )
    args.output.mkdir(parents=True, exist_ok=True)
    blinded = report.pop("blinded")
    for name, value in (
        ("report", report),
        ("blinded-review", blinded["review"]),
        ("review-key", blinded["key"]),
    ):
        (args.output / f"{name}.json").write_text(json.dumps(value, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {"holdout": report["holdout"], "passed": report["automated_holdout_passed"]}, indent=2
        )
    )
    return 0 if report["automated_holdout_passed"] and report["original_g4"]["release_gate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
