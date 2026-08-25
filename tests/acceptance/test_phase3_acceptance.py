import json
from pathlib import Path

import pytest

from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.store import Database
from outpost.transport.governor import AirtimeGovernor
from outpost.transport.simulated import SimulatedRadioLink
from outpost.watch import AlertService, IncidentService


def test_incident_inference_corpus_exceeds_ninety_percent() -> None:
    corpus_path = Path(__file__).parents[1] / "fixtures" / "watch_inference_corpus.json"
    corpus = json.loads(corpus_path.read_text())
    correct = sum(IncidentService.infer(item["text"]) == item["type"] for item in corpus)
    assert len(corpus) == 40
    assert correct / len(corpus) >= 0.90


@pytest.mark.asyncio
async def test_ai_is_not_a_permitted_alert_source(tmp_path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "outpost.db")},
            "channels": {0: {"name": "public"}, 3: {"name": "watch"}},
        }
    )
    service = AlertService(
        database,
        AirtimeGovernor(SimulatedRadioLink(), config.airtime, VirtualClock()),
        VirtualClock(),
        config,
    )
    with pytest.raises(ValueError, match="source"):
        await service.raise_alert("urgent", "Model-authored warning", "ai", source="ai")
    assert await service.list(active_only=False) == []
    await database.close()
