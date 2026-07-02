"""Process-wide shared Telethon client worker for serialized Telegram access."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypeVar

from triak_trade.config.settings import Settings
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.telegram.telethon_client import TelethonTelegramClient

_T = TypeVar("_T")


class SharedTelethonTelegramClient:
    """Serialize Telegram I/O through one Telethon client instance and one worker loop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = TelethonTelegramClient(settings)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._op_lock: asyncio.Lock | None = None

    def _ensure_worker(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._worker_main,
                name="shared-telethon-client",
                daemon=True,
            )
            self._thread.start()
        self._loop_ready.wait(timeout=10)
        if self._loop is None:
            raise RuntimeError("Shared Telegram worker failed to start")

    def _worker_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._op_lock = asyncio.Lock()
        self._loop_ready.set()
        loop.run_forever()
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(self._client.stop())
        loop.close()

    async def fetch_history(
        self,
        channel: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        min_message_id: int | None = None,
    ) -> list[RawTelegramMessage]:
        return await self._submit(
            self._client.fetch_history(
                channel,
                start=start,
                end=end,
                limit=limit,
                min_message_id=min_message_id,
            )
        )

    async def ensure_media_payload(
        self,
        message: RawTelegramMessage,
        *,
        allow_captionless: bool = False,
    ) -> RawTelegramMessage:
        return await self._submit(
            self._client.ensure_media_payload(
                message,
                allow_captionless=allow_captionless,
            )
        )

    async def listen_new_messages(
        self,
        channels: list[str],
        handler: Callable[[RawTelegramMessage], Awaitable[None]],
    ) -> None:
        await self._submit(self._client.listen_new_messages(channels, handler))

    async def forward_message_by_link(
        self,
        message_link: str,
        destination_channel: str,
    ) -> RawTelegramMessage:
        return await self._submit(
            self._client.forward_message_by_link(message_link, destination_channel)
        )

    async def stop(self) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        await self._submit(self._client.stop())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        self._loop = None
        self._thread = None
        self._op_lock = None
        self._loop_ready.clear()

    async def _submit(self, coro: Awaitable[_T]) -> _T:
        self._ensure_worker()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._run_serialized(coro), self._loop)
        return await asyncio.wrap_future(future)

    async def _run_serialized(self, coro: Awaitable[_T]) -> _T:
        assert self._op_lock is not None
        async with self._op_lock:
            return await coro
