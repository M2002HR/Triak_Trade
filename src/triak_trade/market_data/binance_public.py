"""No-auth Binance public historical futures market-data provider."""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from triak_trade.core.logging import log_event
from triak_trade.core.symbols import canonical_market_symbol
from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle
from triak_trade.market_data.errors import (
    MarketDataConnectionError,
    MarketDataHTTPError,
    MarketDataParseError,
    MarketDataTimeoutError,
)
from triak_trade.market_data.intervals import interval_to_milliseconds, validate_interval

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ArchiveSpec:
    kind: str
    label: str
    path: str


class BinancePublicFuturesProvider:
    def __init__(
        self,
        *,
        base_url: str,
        rest_base_url: str,
        klines_path: str,
        ticker_price_path: str,
        cache_dir: str,
        timeout_seconds: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.rest_base_url = rest_base_url.rstrip("/")
        self.klines_path = klines_path
        self.ticker_price_path = ticker_price_path
        self.cache_dir = Path(cache_dir)
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        normalized_symbol = self._normalize_symbol(symbol)
        normalized_interval = validate_interval(interval)
        start_utc = _to_utc(start_time)
        end_utc = _to_utc(end_time)
        if end_utc <= start_utc:
            log_event(
                _log,
                logging.DEBUG,
                "binance_public_market_data.empty_window",
                symbol=normalized_symbol,
                interval=normalized_interval,
                start_time=start_utc.isoformat(),
                end_time=end_utc.isoformat(),
            )
            return []

        specs = _build_archive_specs(start_utc, end_utc, normalized_symbol, normalized_interval)
        if not specs:
            return []
        log_event(
            _log,
            logging.INFO,
            "binance_public_market_data.get_klines.started",
            symbol=normalized_symbol,
            interval=normalized_interval,
            archive_spec_count=len(specs),
            start_time=start_utc.isoformat(),
            end_time=end_utc.isoformat(),
        )

        all_candles: dict[tuple[str, str, datetime, str], Candle] = {}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            for spec in specs:
                payload = await self._load_archive_bytes(client, spec)
                if payload is None:
                    continue
                for candle in self._parse_archive(
                    payload=payload,
                    symbol=normalized_symbol,
                    interval=normalized_interval,
                    start_time=start_utc,
                    end_time=end_utc,
                ):
                    key = (candle.symbol, candle.interval, candle.open_time, candle.source.value)
                    all_candles[key] = candle
        if not all_candles:
            log_event(
                _log,
                logging.INFO,
                "binance_public_market_data.archive_empty_using_rest_fallback",
                symbol=normalized_symbol,
                interval=normalized_interval,
            )
            recent_candles = await self._load_recent_rest_candles(
                symbol=normalized_symbol,
                interval=normalized_interval,
                start_time=start_utc,
                end_time=end_utc,
            )
            for candle in recent_candles:
                key = (candle.symbol, candle.interval, candle.open_time, candle.source.value)
                all_candles[key] = candle
        result = sorted(all_candles.values(), key=lambda item: item.open_time)
        log_event(
            _log,
            logging.INFO,
            "binance_public_market_data.get_klines.completed",
            symbol=normalized_symbol,
            interval=normalized_interval,
            candle_count=len(result),
        )
        return result

    async def get_latest_price(self, symbol: str) -> Decimal:
        normalized_symbol = self._normalize_symbol(symbol)
        log_event(
            _log,
            logging.INFO,
            "binance_public_market_data.get_latest_price.started",
            symbol=normalized_symbol,
        )
        async with httpx.AsyncClient(
            base_url=self.rest_base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                response = await client.get(
                    self.ticker_price_path,
                    params={"symbol": normalized_symbol},
                )
            except httpx.TimeoutException as exc:
                raise MarketDataTimeoutError(
                    "Binance public market data request timed out"
                ) from exc
            except httpx.ConnectError as exc:
                raise MarketDataConnectionError(
                    "Binance public market data connection failed"
                ) from exc

        if response.status_code >= 400:
            raise MarketDataHTTPError(
                f"Binance public market data HTTP error: {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataParseError("Binance public market data JSON parse error") from exc
        if not isinstance(payload, dict) or "price" not in payload:
            raise MarketDataParseError("Unsupported Binance latest price payload format")
        price = Decimal(str(payload["price"]))
        log_event(
            _log,
            logging.INFO,
            "binance_public_market_data.get_latest_price.completed",
            symbol=normalized_symbol,
            price=str(price),
        )
        return price

    def _normalize_symbol(self, symbol: str) -> str:
        normalized = canonical_market_symbol(symbol)
        if not normalized:
            raise MarketDataParseError("Symbol could not be normalized for Binance futures")
        return normalized

    async def _load_archive_bytes(
        self,
        client: httpx.AsyncClient,
        spec: _ArchiveSpec,
    ) -> bytes | None:
        cache_path = self.cache_dir / spec.path
        missing_marker = cache_path.with_suffix(cache_path.suffix + ".missing")
        if cache_path.exists():
            log_event(
                _log,
                logging.DEBUG,
                "binance_public_market_data.archive_cache_hit",
                label=spec.label,
                cache_path=str(cache_path),
            )
            return cache_path.read_bytes()
        if missing_marker.exists():
            log_event(
                _log,
                logging.DEBUG,
                "binance_public_market_data.archive_missing_marker_hit",
                label=spec.label,
                marker_path=str(missing_marker),
            )
            return None

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        log_event(
            _log,
            logging.DEBUG,
            "binance_public_market_data.archive_download_started",
            label=spec.label,
            path=spec.path,
        )
        try:
            response = await client.get("/" + spec.path.lstrip("/"))
        except httpx.TimeoutException as exc:
            raise MarketDataTimeoutError("Binance public archive request timed out") from exc
        except httpx.ConnectError as exc:
            raise MarketDataConnectionError("Binance public archive connection failed") from exc

        if response.status_code == 404:
            missing_marker.write_text(spec.label, encoding="utf-8")
            log_event(
                _log,
                logging.INFO,
                "binance_public_market_data.archive_not_found",
                label=spec.label,
                path=spec.path,
            )
            return None
        if response.status_code >= 400:
            raise MarketDataHTTPError(
                f"Binance public archive HTTP error: {response.status_code}"
            )

        payload = response.content
        cache_path.write_bytes(payload)
        log_event(
            _log,
            logging.DEBUG,
            "binance_public_market_data.archive_cached",
            label=spec.label,
            cache_path=str(cache_path),
            bytes_written=len(payload),
        )
        return payload

    async def _load_recent_rest_candles(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        cache_path = self._recent_cache_path(symbol, interval, start_time, end_time)
        if cache_path.exists():
            log_event(
                _log,
                logging.DEBUG,
                "binance_public_market_data.rest_cache_hit",
                symbol=symbol,
                interval=interval,
                cache_path=str(cache_path),
            )
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return self._parse_rest_rows(
                rows=payload,
                symbol=symbol,
                interval=interval,
                start_time=start_time,
                end_time=end_time,
            )

        interval_ms = interval_to_milliseconds(interval)
        cursor = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        collected_rows: list[list[object]] = []
        log_event(
            _log,
            logging.DEBUG,
            "binance_public_market_data.rest_fetch_started",
            symbol=symbol,
            interval=interval,
        )
        async with httpx.AsyncClient(
            base_url=self.rest_base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            while cursor < end_ms:
                remaining = max(1, ((end_ms - cursor) // interval_ms) + 1)
                limit = min(1500, remaining)
                try:
                    response = await client.get(
                        self.klines_path,
                        params={
                            "symbol": symbol,
                            "interval": interval,
                            "startTime": cursor,
                            "endTime": end_ms,
                            "limit": limit,
                        },
                    )
                except httpx.TimeoutException as exc:
                    raise MarketDataTimeoutError("Binance futures kline request timed out") from exc
                except httpx.ConnectError as exc:
                    raise MarketDataConnectionError(
                        "Binance futures kline connection failed"
                    ) from exc

                if response.status_code == 404:
                    break
                if response.status_code >= 400:
                    raise MarketDataHTTPError(
                        f"Binance futures kline HTTP error: {response.status_code}"
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise MarketDataParseError("Binance futures kline JSON parse error") from exc
                if not isinstance(payload, list):
                    raise MarketDataParseError("Unsupported Binance futures kline payload format")
                if not payload:
                    break
                batch = [row for row in payload if isinstance(row, list)]
                if not batch:
                    break
                collected_rows.extend(batch)
                last_open_time_ms = int(str(batch[-1][0]))
                next_cursor = last_open_time_ms + interval_ms
                if next_cursor <= cursor:
                    break
                cursor = next_cursor

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(collected_rows), encoding="utf-8")
        log_event(
            _log,
            logging.DEBUG,
            "binance_public_market_data.rest_cache_written",
            symbol=symbol,
            interval=interval,
            cache_path=str(cache_path),
            row_count=len(collected_rows),
        )
        return self._parse_rest_rows(
            rows=collected_rows,
            symbol=symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
        )

    def _recent_cache_path(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> Path:
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)
        return (
            self.cache_dir
            / "_rest"
            / symbol
            / interval
            / f"{start_ms}-{end_ms}.json"
        )

    def _parse_archive(
        self,
        *,
        payload: bytes,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = archive.namelist()
                if not names:
                    return []
                with archive.open(names[0], "r") as handle:
                    text_stream = io.TextIOWrapper(handle, encoding="utf-8")
                    reader = csv.reader(text_stream)
                    return self._parse_rest_rows(
                        rows=[[item for item in row] for row in reader],
                        symbol=symbol,
                        interval=interval,
                        start_time=start_time,
                        end_time=end_time,
                    )
        except zipfile.BadZipFile as exc:
            raise MarketDataParseError("Binance archive zip parse error") from exc

    def _parse_rest_rows(
        self,
        *,
        rows: list[list[object]],
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Candle]:
        return [
            candle
            for row in rows
            for candle in [self._parse_row(row=row, symbol=symbol, interval=interval)]
            if candle is not None and start_time <= candle.open_time <= end_time
        ]

    def _parse_row(
        self,
        *,
        row: list[object],
        symbol: str,
        interval: str,
    ) -> Candle | None:
        if row and str(row[0]).strip().lower() == "open_time":
            return None
        if len(row) < 7:
            raise MarketDataParseError("Invalid Binance kline row format")
        try:
            open_time_ms = int(str(row[0]))
            open_price = Decimal(str(row[1]))
            high_price = Decimal(str(row[2]))
            low_price = Decimal(str(row[3]))
            close_price = Decimal(str(row[4]))
            volume = Decimal(str(row[5]))
            close_time_ms = int(str(row[6]))
        except Exception as exc:
            raise MarketDataParseError("Invalid Binance numeric kline values") from exc

        open_time = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
        close_time = datetime.fromtimestamp(close_time_ms / 1000, tz=timezone.utc)
        if close_time <= open_time:
            close_time = open_time + timedelta(milliseconds=interval_to_milliseconds(interval))
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
            source=CandleSource.BINANCE,
        )


def _build_archive_specs(
    start_time: datetime,
    end_time: datetime,
    symbol: str,
    interval: str,
) -> list[_ArchiveSpec]:
    current = _day_start(start_time).date()
    last = _day_start(end_time).date()
    specs: list[_ArchiveSpec] = []
    seen: set[str] = set()
    today = datetime.now(timezone.utc).date()

    while current <= last:
        month_start = current.replace(day=1)
        next_month = _next_month(month_start)
        month_end = next_month - timedelta(days=1)
        month_start_dt = datetime.combine(month_start, time.min, tzinfo=timezone.utc)
        next_month_dt = datetime.combine(next_month, time.min, tzinfo=timezone.utc)
        full_month = (
            start_time <= month_start_dt
            and end_time >= next_month_dt
            and month_end < today
        )
        if full_month:
            label = f"{month_start.year}-{month_start.month:02d}"
            path = (
                f"data/futures/um/monthly/klines/{symbol}/{interval}/"
                f"{symbol}-{interval}-{label}.zip"
            )
            if path not in seen:
                specs.append(_ArchiveSpec(kind="monthly", label=label, path=path))
                seen.add(path)
            current = next_month
            continue

        label = current.isoformat()
        path = (
            f"data/futures/um/daily/klines/{symbol}/{interval}/"
            f"{symbol}-{interval}-{label}.zip"
        )
        if path not in seen:
            specs.append(_ArchiveSpec(kind="daily", label=label, path=path))
            seen.add(path)
        current += timedelta(days=1)
    return specs


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _day_start(value: datetime) -> datetime:
    utc_value = _to_utc(value)
    return datetime.combine(utc_value.date(), time.min, tzinfo=timezone.utc)


def _next_month(current: date) -> date:
    if current.month == 12:
        return date(current.year + 1, 1, 1)
    return date(current.year, current.month + 1, 1)
