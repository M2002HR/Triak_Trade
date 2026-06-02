"""Polling loop for the Telegram admin bot runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from triak_trade.admin_bot.handlers import AdminBotUpdateHandler, OutgoingMessage
from triak_trade.admin_bot.state import AdminBotStateStore, utc_now
from triak_trade.config.settings import Settings
from triak_trade.verification.redaction import redact


class AdminBotRuntimeError(RuntimeError):
    """Admin bot runtime failed safely."""


class TelegramBotPollingClient:
    """Small Telegram Bot API getUpdates/sendMessage wrapper."""

    def __init__(
        self,
        *,
        bot_token: str,
        parse_mode: str,
        disable_web_preview: bool,
        timeout_seconds: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._parse_mode = parse_mode
        self._disable_web_preview = disable_web_preview
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = await self._post("getUpdates", payload)
        result = response.get("result")
        if not isinstance(result, list):
            raise AdminBotRuntimeError("telegram getUpdates result must be a list")
        return [item for item in result if isinstance(item, dict)]

    async def send_outgoing(self, message: OutgoingMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": message.chat_id,
            "text": message.text,
            "parse_mode": self._parse_mode,
            "disable_web_page_preview": self._disable_web_preview,
        }
        if message.reply_markup is not None:
            payload["reply_markup"] = message.reply_markup
        return await self._post("sendMessage", payload)

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._bot_token}/{method}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise AdminBotRuntimeError(f"telegram request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            msg = f"telegram request failed with status {response.status_code}"
            raise AdminBotRuntimeError(msg)
        try:
            data = response.json()
        except ValueError as exc:
            raise AdminBotRuntimeError("telegram response was not valid JSON") from exc
        if not isinstance(data, dict):
            raise AdminBotRuntimeError("telegram response must be a JSON object")
        if data.get("ok") is False:
            raise AdminBotRuntimeError("telegram response ok=false")
        return data


class AdminBotPollingService:
    """Run one polling cycle or a bounded loop."""

    def __init__(
        self,
        *,
        settings: Settings,
        state_store: AdminBotStateStore,
        handler: AdminBotUpdateHandler,
        real: bool,
        client: TelegramBotPollingClient | None = None,
    ) -> None:
        self.settings = settings
        self.state_store = state_store
        self.handler = handler
        self.real = real
        self.client = client

    async def run_once(self, updates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self._mark_running()
        try:
            handled = await self._process_updates(await self._load_updates(updates))
            self._mark_stopped()
            return {"mode": "real" if self.real else "fake", "handled_updates": handled}
        except Exception as exc:
            self._record_error(exc)
            raise

    async def run_loop(self, *, max_runtime_seconds: int | None = None) -> dict[str, Any]:
        self._mark_running(watch=True)
        started = datetime.now(timezone.utc)
        cycles = 0
        handled_total = 0
        try:
            while True:
                updates = await self._load_updates(None)
                handled_total += await self._process_updates(updates)
                cycles += 1
                self._heartbeat()
                if max_runtime_seconds is not None:
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    if elapsed >= max_runtime_seconds:
                        break
                await asyncio.sleep(self.settings.ADMIN_BOT_POLL_INTERVAL_SECONDS)
            self._mark_stopped()
            return {
                "mode": "real" if self.real else "fake",
                "cycles": cycles,
                "handled_updates": handled_total,
            }
        except Exception as exc:
            self._record_error(exc)
            raise

    async def _load_updates(self, updates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if updates is not None:
            return updates
        if not self.real:
            return [_fake_start_update()]
        if self.client is None:
            raise AdminBotRuntimeError("real polling client is required")
        return await self.client.get_updates(
            offset=self.state_store.read_offset(),
            timeout=self.settings.ADMIN_BOT_LONG_POLL_TIMEOUT_SECONDS,
        )

    async def _process_updates(self, updates: list[dict[str, Any]]) -> int:
        handled = 0
        for update in updates:
            result = self.handler.handle_update(update)
            next_offset = (result.update_id + 1) if result.update_id is not None else None
            if next_offset is not None:
                self.state_store.write_offset(next_offset)
            for message in result.outgoing:
                if self.real:
                    if self.client is None:
                        raise AdminBotRuntimeError("real polling client is required")
                    await self.client.send_outgoing(message)
                else:
                    self.state_store.append_log(
                        "admin_bot.fake_outgoing",
                        {
                            "chat_id": message.chat_id,
                            "text_preview": message.text[:120],
                            "reply_markup_present": message.reply_markup is not None,
                        },
                    )
            self.state_store.append_log(
                "admin_bot.update_handled",
                redact(
                    {
                        "update_id": result.update_id,
                        "username": result.username,
                        "authorized": result.authorized,
                        "outgoing_count": len(result.outgoing),
                        "notes": result.notes,
                    }
                ),
            )
            handled += 1
        return handled

    def _mark_running(self, *, watch: bool = False) -> None:
        state = self.state_store.read_status()
        state.running = True
        state.pid = None
        state.started_at = state.started_at or utc_now()
        state.last_heartbeat_at = utc_now()
        state.mode = "real" if self.real else "fake"
        state.watch = watch
        self.state_store.write_status(state)
        self.state_store.append_log(
            "admin_bot.runtime_started",
            {"mode": state.mode, "watch": watch},
        )

    def _mark_stopped(self) -> None:
        state = self.state_store.read_status()
        state.running = False
        state.last_heartbeat_at = utc_now()
        self.state_store.write_status(state)
        self.state_store.append_log("admin_bot.runtime_stopped", {"mode": state.mode})

    def _heartbeat(self) -> None:
        state = self.state_store.read_status()
        state.last_heartbeat_at = utc_now()
        self.state_store.write_status(state)
        self.state_store.append_log("admin_bot.heartbeat", {"mode": state.mode})

    def _record_error(self, exc: Exception) -> None:
        state = self.state_store.read_status()
        state.running = False
        state.last_heartbeat_at = utc_now()
        state.last_error_type = type(exc).__name__
        state.last_error_message_redacted = str(exc)
        self.state_store.write_status(state)
        self.state_store.append_log(
            "admin_bot.runtime_error",
            {"error_type": type(exc).__name__, "error": str(exc)},
        )


def _fake_start_update() -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1,
            "text": "/start",
            "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "is_bot": False, "username": "we_are_waiting_for_him"},
        },
    }
