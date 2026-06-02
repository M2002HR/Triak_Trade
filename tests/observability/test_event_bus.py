from __future__ import annotations

from triak_trade.config.settings import Settings
from triak_trade.observability.event_bus import ProcessingEventBus
from triak_trade.observability.processing_audit import build_sample_processing_audit_event


def test_event_bus_publishes_to_subscribers() -> None:
    bus = ProcessingEventBus()
    seen: list[str] = []
    event = build_sample_processing_audit_event(Settings(_env_file=None))

    bus.subscribe(lambda item: seen.append(item.event_id))
    bus.publish(event)

    assert bus.handler_count == 1
    assert seen == ["audit_sample"]
