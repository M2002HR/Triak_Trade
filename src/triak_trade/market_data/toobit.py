"""Toobit public market data provider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from triak_trade.core.symbols import (
    futures_contract_symbol_candidates,
    futures_index_symbol_candidates,
)
from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle
from triak_trade.market_data.errors import (
    MarketDataConnectionError,
    MarketDataHTTPError,
    MarketDataParseError,
    MarketDataTimeoutError,
)
from triak_trade.market_data.intervals import interval_to_milliseconds, validate_interval


class ToobitMarketDataProvider:
    def __init__(
        self,
        *,
        base_url: str,
        klines_path: str,
        mark_price_klines_path: str = "/quote/v1/markPrice/klines",
        index_klines_path: str = "/quote/v1/index/klines",
        contract_ticker_price_path: str = "/quote/v1/contract/ticker/price",
        timeout_seconds: int,
        limit: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.klines_path = klines_path
        self.mark_price_klines_path = mark_price_klines_path
        self.index_klines_path = index_klines_path
        self.contract_ticker_price_path = contract_ticker_price_path
        self.timeout_seconds = timeout_seconds
        self.limit = limit
        self.transport = transport

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        normalized_interval = validate_interval(interval)
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        if end_ms <= start_ms:
            return []

        contract_symbols = futures_contract_symbol_candidates(symbol)
        index_symbols = futures_index_symbol_candidates(symbol)
        attempts = [
            _MarketDataAttempt(
                path=self.klines_path,
                symbols=contract_symbols,
                mode="contract",
            ),
            _MarketDataAttempt(
                path=self.mark_price_klines_path,
                symbols=contract_symbols,
                mode="mark_price",
            ),
            _MarketDataAttempt(
                path=self.index_klines_path,
                symbols=index_symbols,
                mode="index",
            ),
        ]
        last_error: Exception | None = None

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            for attempt in attempts:
                for candidate_symbol in attempt.symbols:
                    try:
                        candles = await self._fetch_series(
                            client=client,
                            attempt=attempt,
                            symbol=candidate_symbol,
                            interval=normalized_interval,
                            start_ms=start_ms,
                            end_ms=end_ms,
                        )
                    except (
                        MarketDataConnectionError,
                        MarketDataHTTPError,
                        MarketDataParseError,
                        MarketDataTimeoutError,
                    ) as exc:
                        last_error = exc
                        continue
                    if candles:
                        return candles

        if last_error is not None:
            raise last_error
        return []

    async def get_latest_price(self, symbol: str) -> Decimal:
        candidates = futures_contract_symbol_candidates(symbol)
        if not candidates:
            raise MarketDataParseError("No futures symbol candidate could be derived")

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            last_error: Exception | None = None
            for candidate in candidates:
                try:
                    response = await client.get(
                        self.contract_ticker_price_path,
                        params={"symbol": candidate},
                    )
                except httpx.TimeoutException as exc:
                    raise MarketDataTimeoutError("Toobit market data request timed out") from exc
                except httpx.ConnectError as exc:
                    raise MarketDataConnectionError("Toobit market data connection failed") from exc

                if response.status_code >= 400:
                    last_error = MarketDataHTTPError(
                        f"Toobit market data HTTP error: {response.status_code}"
                    )
                    continue

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise MarketDataParseError("Toobit market data JSON parse error") from exc

                if isinstance(payload, list) and payload:
                    price = payload[0].get("p")
                    if price is not None:
                        return Decimal(str(price))
                last_error = MarketDataParseError("Unsupported Toobit contract ticker payload")

        if last_error is not None:
            raise last_error
        raise MarketDataParseError("No latest price was returned by Toobit")

    async def _fetch_series(
        self,
        *,
        client: httpx.AsyncClient,
        attempt: _MarketDataAttempt,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[Candle]:
        chunk_limit = max(self.limit, 1)
        if attempt.mode != "contract":
            chunk_limit = max(chunk_limit, 2000)
        chunk_ms = interval_to_milliseconds(interval) * chunk_limit
        all_candles: dict[tuple[str, str, datetime, str], Candle] = {}

        cursor = start_ms
        while cursor < end_ms:
            chunk_end = min(end_ms, cursor + chunk_ms)
            rows = await self._fetch_chunk(
                client=client,
                path=attempt.path,
                symbol=symbol,
                interval=interval,
                start_ms=cursor,
                end_ms=chunk_end,
                mode=attempt.mode,
            )
            for row in rows:
                candle = self._parse_candle_row(
                    row=row,
                    symbol=symbol,
                    interval=interval,
                    mode=attempt.mode,
                )
                key = (
                    candle.symbol,
                    candle.interval,
                    candle.open_time,
                    candle.source.value,
                )
                all_candles[key] = candle
            cursor = chunk_end

        return sorted(all_candles.values(), key=lambda item: item.open_time)

    async def _fetch_chunk(
        self,
        *,
        client: httpx.AsyncClient,
        path: str,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        mode: str,
    ) -> list[Any]:
        params = _build_request_params(
            symbol=symbol,
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=self.limit,
            mode=mode,
        )
        try:
            response = await client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise MarketDataTimeoutError("Toobit market data request timed out") from exc
        except httpx.ConnectError as exc:
            raise MarketDataConnectionError("Toobit market data connection failed") from exc

        if response.status_code >= 400:
            raise MarketDataHTTPError(f"Toobit market data HTTP error: {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataParseError("Toobit market data JSON parse error") from exc

        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
            result = payload.get("result")
            if isinstance(result, list):
                return result
            if data is None and result is None:
                return []
        raise MarketDataParseError("Unsupported Toobit kline payload format")

    @staticmethod
    def _parse_candle_row(*, row: Any, symbol: str, interval: str, mode: str) -> Candle:
        open_time_ms: int
        open_price: Decimal
        high_price: Decimal
        low_price: Decimal
        close_price: Decimal
        volume: Decimal
        close_time_ms: int | None = None

        try:
            if isinstance(row, list) and len(row) >= 6:
                open_time_ms = int(str(row[0]))
                open_price = Decimal(str(row[1]))
                high_price = Decimal(str(row[2]))
                low_price = Decimal(str(row[3]))
                close_price = Decimal(str(row[4]))
                volume = Decimal(str(row[5]))
                close_time_ms = int(str(row[6])) if len(row) > 6 and row[6] is not None else None
            elif isinstance(row, dict) and mode == "mark_price":
                open_time_ms = int(str(row["time"]))
                open_price = Decimal(str(row["open"]))
                high_price = Decimal(str(row["high"]))
                low_price = Decimal(str(row["low"]))
                close_price = Decimal(str(row["close"]))
                volume = Decimal(str(row.get("volume", "0")))
            elif isinstance(row, dict) and mode == "index":
                open_time_ms = int(str(row["t"]))
                open_price = Decimal(str(row["o"]))
                high_price = Decimal(str(row["h"]))
                low_price = Decimal(str(row["l"]))
                close_price = Decimal(str(row["c"]))
                volume = Decimal(str(row.get("v", "0")))
            else:
                raise MarketDataParseError("Invalid kline row format")
        except KeyError as exc:
            raise MarketDataParseError("Missing kline field") from exc
        except Exception as exc:
            if isinstance(exc, MarketDataParseError):
                raise
            raise MarketDataParseError("Invalid numeric kline values") from exc

        open_time = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
        if close_time_ms is None:
            close_time = open_time + timedelta(milliseconds=interval_to_milliseconds(interval))
        else:
            close_time = datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc)
            if close_time <= open_time:
                close_time = open_time + timedelta(
                    milliseconds=interval_to_milliseconds(interval)
                )

        return Candle(
            symbol=symbol,
            interval=interval,
            open_time=open_time,
            close_time=close_time,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            source=CandleSource.TOOBIT,
        )


class _MarketDataAttempt:
    def __init__(self, *, path: str, symbols: list[str], mode: str) -> None:
        self.path = path
        self.symbols = symbols
        self.mode = mode


def _build_request_params(
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int,
    mode: str,
) -> dict[str, str | int]:
    if mode == "contract":
        return {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": min(max(limit, 1), 1000),
        }
    return {
        "symbol": symbol,
        "interval": interval,
        "from": start_ms,
        "to": end_ms,
        "limit": min(max(limit, 1), 2000),
    }
