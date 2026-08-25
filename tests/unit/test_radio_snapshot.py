from outpost.transport.models import RadioSnapshot


def test_radio_snapshot_is_immutable() -> None:
    snapshot = RadioSnapshot(
        "!12345678", "US", "LONG_FAST", frozenset({0, 2, 3}), 40.4406, -79.9959
    )
    assert snapshot.channels == {0, 2, 3}
    assert snapshot.latitude == 40.4406 and snapshot.longitude == -79.9959
