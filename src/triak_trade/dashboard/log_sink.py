"""Shared dashboard log sink helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from triak_trade.config.settings import Settings
from triak_trade.verification.redaction import redact


def append_dashboard_log(
    settings: Settings,
    event: str,
    payload: dict[str, Any],
    *,
    level: str = "INFO",
    module: str | None = None,
) -> None:
    path = Path(settings.DASHBOARD_LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        redact(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": level.upper(),
                "module": module or _module_from_event(event),
                "event": event,
                "payload": payload,
            }
        ),
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


def _module_from_event(event: str) -> str:
    if "." not in event:
        return "runtime"
    return event.split(".", 1)[0]
