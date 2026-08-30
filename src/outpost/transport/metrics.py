from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

RADIO_RECONNECTS = Counter("outpost_radio_reconnects_total", "Radio reconnect attempts")
INBOUND = Counter(
    "outpost_inbound_messages_total",
    "Normalised inbound messages",
    ("portnum", "channel", "direct"),
)
INBOUND_DROPPED = Counter("outpost_inbound_dropped_total", "Dropped inbound messages", ("reason",))
INBOUND_HANDLER_FAILURES = Counter(
    "outpost_inbound_handler_failures_total",
    "Inbound messages contained after an application handler failure",
    ("exception_type",),
)
INBOUND_QUEUE_DEPTH = Gauge("outpost_inbound_queue_depth", "Inbound queue depth", ("lane",))
INBOUND_WORKERS_BUSY = Gauge("outpost_inbound_workers_busy", "Busy inbound workers")
SAFETY_FLOOR_ATTEMPTS = Counter(
    "outpost_safety_floor_attempts_total",
    "Safety-floor command decisions",
    ("command", "outcome"),
)
SAFETY_NOTIFICATION_DELIVERY = Counter(
    "outpost_safety_notification_delivery_total",
    "Safety notification admissions by audience and outcome",
    ("purpose", "audience", "outcome"),
)
COMMAND_REPLY_DELIVERY = Counter(
    "outpost_command_reply_delivery_total",
    "Command reply admission outcomes",
    ("outcome",),
)
OUTBOUND_ENQUEUED = Counter(
    "outpost_outbound_enqueued_total", "Queued outbound messages", ("class",)
)
OUTBOUND_SENT = Counter(
    "outpost_outbound_sent_total", "Sent outbound messages", ("class", "dest_type")
)
OUTBOUND_DROPPED = Counter(
    "outpost_outbound_dropped_total", "Dropped outbound messages", ("class", "reason")
)
QUEUE_DEPTH = Gauge("outpost_outbound_queue_depth", "Outbound queue depth", ("class",))
AIRTIME_USED = Gauge("outpost_airtime_used_ratio", "Own rolling one-hour airtime ratio")
CHANNEL_UTIL = Gauge("outpost_channel_utilisation_ratio", "Radio channel utilisation ratio")
AIR_UTIL_TX = Gauge("outpost_air_util_tx_ratio", "Radio-reported transmit airtime ratio")
RADIO_BATTERY_LEVEL = Gauge(
    "outpost_radio_battery_level_percent",
    "Connected radio battery percentage; NaN when no battery is reported",
)
RADIO_BATTERY_REPORTED = Gauge(
    "outpost_radio_battery_reported",
    "Whether the connected radio currently reports a battery percentage",
)
RADIO_POWER_OBSERVATION_FAILURES = Counter(
    "outpost_radio_power_observation_failures_total",
    "Failures while recording connected-radio power history",
)
TOA_SECONDS = Histogram("outpost_toa_seconds", "Estimated packet time on air")
ACK_OUTCOME = Counter(
    "outpost_ack_outcome_total", "Outbound acknowledgement outcomes", ("outcome",)
)
