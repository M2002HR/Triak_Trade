"""Simple synchronous event bus for audit events."""

from __future__ import annotations

from collections.abc import Callable

from triak_trade.observability.events import ProcessingAuditEvent

ProcessingAuditHandler = Callable[[ProcessingAuditEvent], None]


class ProcessingEventBus:
    def __init__(self) -> None:
        self._handlers: list[ProcessingAuditHandler] = []

    def subscribe(self, handler: ProcessingAuditHandler) -> None:
        self._handlers.append(handler)

    def publish(self, event: ProcessingAuditEvent) -> None:
        for handler in list(self._handlers):
            handler(event)

    @property
    def handler_count(self) -> int:
        return len(self._handlers)
