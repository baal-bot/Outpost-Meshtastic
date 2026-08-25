from datetime import UTC, datetime

from outpost.transport.inbound import InboundPipeline
from outpost.transport.models import InboundMessage


def packet(sender: str, packet_id: int) -> InboundMessage:
    return InboundMessage(packet_id, sender, "!self", 0, 1, True, "PING", None, datetime.now(UTC))


def test_self_and_duplicate_packets_are_dropped() -> None:
    pipeline = InboundPipeline("!self")
    assert pipeline.process(packet("!self", 1)) is None
    assert pipeline.process(packet("!peer", 2)) is not None
    assert pipeline.process(packet("!peer", 2)) is None


def test_bridge_packets_are_marked_no_reply() -> None:
    pipeline = InboundPipeline("!self", {"!bridge"})
    assert pipeline.process(packet("!bridge", 3)).no_reply is True
