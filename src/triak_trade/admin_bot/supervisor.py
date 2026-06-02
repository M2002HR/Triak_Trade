"""Small restart supervisor for the admin bot runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from triak_trade.admin_bot.state import AdminBotStateStore
from triak_trade.config.settings import Settings


class AdminBotSupervisor:
    """Restart a runtime coroutine after crashes within configured limits."""

    def __init__(self, *, settings: Settings, state_store: AdminBotStateStore) -> None:
        self.settings = settings
        self.state_store = state_store

    async def run(
        self,
        runtime_factory: Callable[[], Awaitable[dict[str, Any]]],
        *,
        max_runtime_seconds: int | None = None,
    ) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        restarts = 0
        last_result: dict[str, Any] = {}
        while True:
            try:
                last_result = await runtime_factory()
                break
            except Exception as exc:
                restarts += 1
                self._record_restart(restarts, exc)
                if not self.settings.ADMIN_BOT_SUPERVISOR_RESTART_ON_CRASH:
                    raise
                if restarts > self.settings.ADMIN_BOT_SUPERVISOR_MAX_RESTARTS:
                    raise
                if max_runtime_seconds is not None:
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    if elapsed >= max_runtime_seconds:
                        break
                await asyncio.sleep(self.settings.ADMIN_BOT_SUPERVISOR_RESTART_DELAY_SECONDS)
        last_result["restart_count"] = restarts
        return last_result

    def _record_restart(self, restarts: int, exc: Exception) -> None:
        state = self.state_store.read_status()
        state.restart_count = restarts
        state.last_error_type = type(exc).__name__
        state.last_error_message_redacted = str(exc)
        self.state_store.write_status(state)
        self.state_store.append_log(
            "admin_bot.supervisor_restart",
            {"restart_count": restarts, "error_type": type(exc).__name__},
        )
