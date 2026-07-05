"""Toobit HTTP client for public and signed endpoints."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from triak_trade.core.logging import log_event
from triak_trade.exchange.toobit.errors import (
    ToobitAPIError,
    ToobitConnectionError,
    ToobitParseError,
    ToobitTimeoutError,
)
from triak_trade.exchange.toobit.signer import ToobitSigner

log = logging.getLogger(__name__)


class ToobitClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout_seconds: int,
        recv_window: int,
        time_path: str,
        exchange_info_path: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout_seconds = timeout_seconds
        self.recv_window = recv_window
        self.time_path = time_path
        self.exchange_info_path = exchange_info_path
        self.transport = transport
        self.signer = ToobitSigner(api_secret)
        self._server_time_offset_ms = 0

    def _request_fields(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, object] | None,
        data: dict[str, object] | None,
        signed: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        safe_params: dict[str, object] = {}
        for key, value in (params or {}).items():
            if key in {"signature", "timestamp", "recvWindow"}:
                continue
            safe_params[key] = value if isinstance(value, (str, int, float, bool)) else str(value)
        return {
            "method": method.upper(),
            "path": path,
            "signed": signed,
            "param_keys": sorted((params or {}).keys()),
            "params": safe_params or None,
            "data_keys": sorted((data or {}).keys()),
            "has_data": bool(data),
            "timeout_seconds": self.timeout_seconds,
            "has_auth_header": bool(extra_headers and "X-BB-APIKEY" in extra_headers),
            "server_time_offset_ms": self._server_time_offset_ms,
        }

    async def get_server_time(self) -> dict[str, Any]:
        return await self.public_request("GET", self.time_path)

    async def get_exchange_info(self) -> dict[str, Any]:
        return await self.public_request("GET", self.exchange_info_path)

    async def public_request(
        self,
        method: str,
        path: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        return await self._request(method=method, path=path, params=params, data=None, signed=False)

    async def signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(2):
            signed_params = dict(params or {})
            signed_params["timestamp"] = self._current_timestamp_ms()
            if self.recv_window > 0:
                signed_params["recvWindow"] = self.recv_window
            signature = self.signer.sign(signed_params)
            signed_params["signature"] = signature
            headers = {"X-BB-APIKEY": self.api_key}
            try:
                return await self._request(
                    method=method,
                    path=path,
                    params=signed_params,
                    data=data,
                    signed=True,
                    extra_headers=headers,
                )
            except ToobitAPIError as exc:
                if exc.error_code != -1021 or attempt > 0:
                    log_event(
                        log,
                        logging.ERROR,
                        "toobit.signed_request_failed",
                        attempt=attempt + 1,
                        error_code=exc.error_code,
                        status_code=exc.status_code,
                        error_message=str(exc),
                        **self._request_fields(
                            method=method,
                            path=path,
                            params=signed_params,
                            data=data,
                            signed=True,
                            extra_headers=headers,
                        ),
                    )
                    raise
                log_event(
                    log,
                    logging.WARNING,
                    "toobit.timestamp_resync_requested",
                    attempt=attempt + 1,
                    error_code=exc.error_code,
                    status_code=exc.status_code,
                    error_message=str(exc),
                    **self._request_fields(
                        method=method,
                        path=path,
                        params=signed_params,
                        data=data,
                        signed=True,
                        extra_headers=headers,
                    ),
                )
                await self._sync_server_time_offset()
        raise ToobitAPIError("Toobit signed request failed after timestamp resync")

    def _current_timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._server_time_offset_ms

    async def _sync_server_time_offset(self) -> None:
        previous_offset = self._server_time_offset_ms
        payload = await self.get_server_time()
        server_time = self._extract_server_time_ms(payload)
        self._server_time_offset_ms = server_time - int(time.time() * 1000)
        log_event(
            log,
            logging.INFO,
            "toobit.server_time_offset_synced",
            previous_offset_ms=previous_offset,
            new_offset_ms=self._server_time_offset_ms,
            server_time_ms=server_time,
        )

    @staticmethod
    def _extract_server_time_ms(payload: dict[str, Any]) -> int:
        raw = payload.get("serverTime")
        if raw is None and isinstance(payload.get("data"), dict):
            raw = payload["data"].get("serverTime")
        if raw is None:
            raise ToobitParseError("Toobit server time payload missing serverTime")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ToobitParseError("Toobit server time is not a valid integer") from exc

    async def _request(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, object] | None,
        data: dict[str, object] | None,
        signed: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        query_params: dict[str, str | int | float | bool | None] | None = None
        if params is not None:
            query_params = {
                key: (
                    value
                    if isinstance(value, (str, int, float, bool)) or value is None
                    else str(value)
                )
                for key, value in params.items()
            }
        request_fields = self._request_fields(
            method=method,
            path=path,
            params=params,
            data=data,
            signed=signed,
            extra_headers=extra_headers,
        )
        started_at = time.perf_counter()
        log_event(log, logging.DEBUG, "toobit.request_started", **request_fields)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method=method.upper(),
                    url=path,
                    params=query_params,
                    data=data,
                    headers=extra_headers,
                )
        except httpx.TimeoutException as exc:
            log_event(
                log,
                logging.WARNING,
                "toobit.request_timeout",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                error_message=str(exc),
                **request_fields,
            )
            raise ToobitTimeoutError("Toobit request timed out") from exc
        except httpx.ConnectError as exc:
            log_event(
                log,
                logging.WARNING,
                "toobit.request_connection_failed",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                error_message=str(exc),
                **request_fields,
            )
            raise ToobitConnectionError("Toobit connection failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            if response.status_code >= 400:
                log_event(
                    log,
                    logging.ERROR,
                    "toobit.request_http_error_non_json",
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                    status_code=response.status_code,
                    response_preview=response.text[:200],
                    **request_fields,
                )
                raise ToobitAPIError(
                    f"Toobit API HTTP error: {response.status_code}: {response.text}",
                    status_code=response.status_code,
                    payload=response.text,
                ) from exc
            log_event(
                log,
                logging.ERROR,
                "toobit.request_parse_failed",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                status_code=response.status_code,
                response_preview=response.text[:200],
                **request_fields,
            )
            raise ToobitParseError("Toobit response is not valid JSON") from exc

        if response.status_code >= 400:
            if isinstance(payload, dict):
                code = payload.get("code")
                msg = payload.get("msg") or payload.get("message") or ""
                log_event(
                    log,
                    logging.ERROR,
                    "toobit.request_http_error",
                    elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                    status_code=response.status_code,
                    response_code=code,
                    response_message=msg,
                    payload_type=type(payload).__name__,
                    **request_fields,
                )
                raise ToobitAPIError(
                    f"Toobit API HTTP error: {response.status_code}: {msg}".rstrip(),
                    status_code=response.status_code,
                    error_code=code,
                    payload=payload,
                )
            log_event(
                log,
                logging.ERROR,
                "toobit.request_http_error",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                status_code=response.status_code,
                payload_type=type(payload).__name__,
                **request_fields,
            )
            raise ToobitAPIError(
                f"Toobit API HTTP error: {response.status_code}",
                status_code=response.status_code,
                payload=payload,
            )

        if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0", 200):
            log_event(
                log,
                logging.ERROR,
                "toobit.request_api_error",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                status_code=response.status_code,
                response_code=payload.get("code"),
                response_message=payload.get("msg", ""),
                **request_fields,
            )
            raise ToobitAPIError(
                f"Toobit API error code {payload.get('code')}: {payload.get('msg', '')}",
                status_code=response.status_code,
                error_code=payload.get("code"),
                payload=payload,
            )

        # Futures endpoints return lists directly (e.g. /api/v1/futures/balance)
        if isinstance(payload, (dict, list)):
            log_event(
                log,
                logging.DEBUG,
                "toobit.request_completed",
                elapsed_ms=int((time.perf_counter() - started_at) * 1000),
                status_code=response.status_code,
                payload_type=type(payload).__name__,
                payload_size=(len(payload) if isinstance(payload, list) else len(payload.keys())),
                response_code=payload.get("code") if isinstance(payload, dict) else None,
                **request_fields,
            )
            return payload  # type: ignore[return-value]
        log_event(
            log,
            logging.ERROR,
            "toobit.request_unexpected_payload_type",
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
            status_code=response.status_code,
            payload_type=type(payload).__name__,
            **request_fields,
        )
        raise ToobitParseError("Toobit response must be JSON object or array")

    def __repr__(self) -> str:
        return "ToobitClient(api_key=**redacted**, api_secret=**redacted**)"
