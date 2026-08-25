from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace

from .models import InboundMessage


class InboundPipeline:
    def __init__(self, local_node_id: str, bridge_node_ids: set[str] | None = None) -> None:
        self.local_node_id = local_node_id
        self.bridge_node_ids = bridge_node_ids or set()
        self._seen: OrderedDict[tuple[str, int], None] = OrderedDict()
        self.dropped: dict[str, int] = {"self": 0, "duplicate": 0}

    def process(self, message: InboundMessage) -> InboundMessage | None:
        if message.from_id == self.local_node_id:
            self.dropped["self"] += 1
            return None
        key = (message.from_id, message.packet_id)
        if key in self._seen:
            self.dropped["duplicate"] += 1
            return None
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > 2048:
            self._seen.popitem(last=False)
        return replace(message, no_reply=message.from_id in self.bridge_node_ids)
