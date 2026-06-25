"""Local in-process ASGI client for dashboard smoke tests and unit tests."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI


class LocalASGIClient:
    """Synchronous wrapper over ``httpx.ASGITransport`` for local app requests."""

    def __init__(self, app: FastAPI, *, base_url: str = "http://testserver") -> None:
        self._app = app
        self._base_url = base_url

    async def _request_async(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=self._app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=self._base_url,
        ) as client:
            return await client.request(method, url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        return asyncio.run(self._request_async(method, url, kwargs))

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)
