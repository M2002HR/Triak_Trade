"""Tests for corrected Toobit futures client."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from triak_trade.exchange.toobit.futures import (
    FuturesAccountInfo,
    FuturesBalance,
    FuturesOrder,
    FuturesPosition,
    ToobitFuturesClient,
    _dec,
    _int,
    from_futures_symbol,
    to_futures_symbol,
)


def _make_client(signed_response: Any = None, public_response: Any = None) -> ToobitFuturesClient:
    mock_base = MagicMock()
    mock_base.signed_request = AsyncMock(return_value=signed_response or {})
    mock_base.public_request = AsyncMock(return_value=public_response or {})
    return ToobitFuturesClient(client=mock_base)


class TestSymbolHelpers:
    def test_btcusdt_to_futures(self) -> None:
        assert to_futures_symbol("BTCUSDT") == "BTC-SWAP-USDT"

    def test_ethusdt_to_futures(self) -> None:
        assert to_futures_symbol("ETHUSDT") == "ETH-SWAP-USDT"

    def test_already_swap_format(self) -> None:
        assert to_futures_symbol("BTC-SWAP-USDT") == "BTC-SWAP-USDT"

    def test_from_futures_btc(self) -> None:
        assert from_futures_symbol("BTC-SWAP-USDT") == "BTCUSDT"

    def test_from_futures_passthrough(self) -> None:
        assert from_futures_symbol("BTCUSDT") == "BTCUSDT"


class TestHelpers:
    def test_dec_valid(self) -> None:
        assert _dec("100.50") == Decimal("100.50")

    def test_dec_zero_on_invalid(self) -> None:
        assert _dec("abc") == Decimal("0")
        assert _dec(None) == Decimal("0")

    def test_int_helper(self) -> None:
        assert _int("10") == 10
        assert _int("10.5") == 10
        assert _int("abc") == 0


class TestFuturesBalance:
    def test_parse_balance(self) -> None:
        raw = {
            "coin": "USDT",
            "balance": "500.00",
            "availableBalance": "400.00",
            "positionMargin": "100.00",
            "orderMargin": "0",
            "crossUnRealizedPnl": "25.50",
            "coupon": "0",
        }
        b = FuturesBalance(raw)
        assert b.coin == "USDT"
        assert b.balance == Decimal("500.00")
        assert b.available_balance == Decimal("400.00")
        assert b.cross_unrealized_pnl == Decimal("25.50")


class TestFuturesAccountInfo:
    def _make_info(self) -> FuturesAccountInfo:
        balances = [FuturesBalance({
            "coin": "USDT",
            "balance": "1000",
            "availableBalance": "800",
            "positionMargin": "200",
            "orderMargin": "0",
            "crossUnRealizedPnl": "50",
            "coupon": "0",
        })]
        return FuturesAccountInfo(
            balances=balances,
            today_pnl={"data": {"dayProfit": "15.5", "dayProfitRate": "0.0155"}},
            api_key_type="master",
            user_id="12345",
        )

    def test_wallet_balance(self) -> None:
        info = self._make_info()
        assert info.total_wallet_balance == Decimal("1000")

    def test_available_balance(self) -> None:
        info = self._make_info()
        assert info.available_balance == Decimal("800")

    def test_day_profit(self) -> None:
        info = self._make_info()
        assert info.day_profit == Decimal("15.5")

    def test_usdt_balance(self) -> None:
        info = self._make_info()
        usdt = info.usdt_balance()
        assert usdt is not None
        assert usdt.coin == "USDT"

    def test_no_usdt_returns_first(self) -> None:
        balances = [FuturesBalance({"coin": "BTC", "balance": "0.5"})]
        info = FuturesAccountInfo(balances=balances)
        b = info.usdt_balance()
        assert b is not None  # falls back to first balance


class TestFuturesPosition:
    def test_parse_open_long(self) -> None:
        raw = {
            "symbol": "BTC-SWAP-USDT",
            "side": "LONG",
            "avgPrice": "50000",
            "position": "0.5",
            "available": "0.5",
            "leverage": "10",
            "lastPrice": "51000",
            "unrealizedPnL": "500",
            "realizedPnL": "0",
            "markPrice": "51000",
        }
        pos = FuturesPosition(raw)
        assert pos.symbol == "BTC-SWAP-USDT"
        assert pos.symbol_internal == "BTCUSDT"
        assert pos.side == "LONG"
        assert pos.is_long
        assert pos.is_open
        assert pos.leverage == 10
        assert pos.avg_price == Decimal("50000")

    def test_closed_position(self) -> None:
        pos = FuturesPosition({"symbol": "ETH-SWAP-USDT", "position": "0"})
        assert not pos.is_open

    def test_short_side(self) -> None:
        pos = FuturesPosition({"symbol": "BTC-SWAP-USDT", "side": "SHORT", "position": "1"})
        assert not pos.is_long
        assert pos.is_open


class TestFuturesOrder:
    def test_parse_order(self) -> None:
        raw = {
            "orderId": "abc123",
            "clientOrderId": "myorder",
            "symbol": "BTC-SWAP-USDT",
            "side": "BUY_OPEN",
            "type": "MARKET",
            "origQty": "1",
            "executedQty": "1",
            "avgPrice": "50000",
            "status": "FILLED",
            "leverage": "10",
        }
        order = FuturesOrder(raw)
        assert order.order_id == "abc123"
        assert order.status == "FILLED"
        assert order.avg_price == Decimal("50000")
        assert order.leverage == 10

    def test_parse_order_from_data_wrapper(self) -> None:
        raw = {"data": {
            "orderId": "x1",
            "symbol": "BTC-SWAP-USDT",
            "side": "SELL_OPEN",
            "status": "NEW",
        }}
        order = FuturesOrder(raw)
        assert order.order_id == "x1"


class TestToobitFuturesClientAPI:
    @pytest.mark.asyncio
    async def test_get_futures_balance(self) -> None:
        response = [
            {
                "coin": "USDT",
                "balance": "1000",
                "availableBalance": "900",
                "positionMargin": "100",
                "orderMargin": "0",
                "crossUnRealizedPnl": "0",
                "coupon": "0",
            }
        ]
        client = _make_client(signed_response=response)
        balances = await client.get_futures_balance()
        assert len(balances) == 1
        assert balances[0].balance == Decimal("1000")
        client._client.signed_request.assert_called_once_with("GET", "/api/v1/futures/balance")

    @pytest.mark.asyncio
    async def test_get_positions_empty(self) -> None:
        client = _make_client(signed_response=[])
        positions = await client.get_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_get_positions_with_data(self) -> None:
        response = [{"symbol": "BTC-SWAP-USDT", "side": "LONG", "position": "0.1",
                     "avgPrice": "50000", "leverage": "10"}]
        client = _make_client(signed_response=response)
        positions = await client.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "BTC-SWAP-USDT"

    @pytest.mark.asyncio
    async def test_get_open_positions_filters_zero(self) -> None:
        response = [
            {"symbol": "BTC-SWAP-USDT", "position": "0.1", "side": "LONG"},
            {"symbol": "ETH-SWAP-USDT", "position": "0"},
        ]
        client = _make_client(signed_response=response)
        open_pos = await client.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].symbol == "BTC-SWAP-USDT"

    @pytest.mark.asyncio
    async def test_place_order_buy_open(self) -> None:
        response = {"orderId": "ord1", "side": "BUY_OPEN", "type": "MARKET",
                    "origQty": "1", "executedQty": "1", "avgPrice": "50000", "status": "FILLED"}
        client = _make_client(signed_response=response)
        order = await client.place_order(
            symbol="BTCUSDT",
            side="BUY_OPEN",
            order_type="MARKET",
            quantity=Decimal("1"),
            leverage=10,
        )
        assert order.order_id == "ord1"
        call_kwargs = client._client.signed_request.call_args
        params = call_kwargs[1]["params"] if call_kwargs[1] else call_kwargs[0][2]
        assert params["symbol"] == "BTC-SWAP-USDT"
        assert params["side"] == "BUY_OPEN"

    @pytest.mark.asyncio
    async def test_open_long_uses_buy_open(self) -> None:
        response = {"orderId": "long1", "side": "BUY_OPEN", "status": "FILLED",
                    "origQty": "1", "executedQty": "1", "avgPrice": "50000", "type": "MARKET"}
        client = _make_client(signed_response=response)
        order = await client.open_long(symbol="BTCUSDT", quantity=Decimal("1"), leverage=10)
        assert order.order_id == "long1"
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["side"] == "BUY_OPEN"

    @pytest.mark.asyncio
    async def test_open_short_uses_sell_open(self) -> None:
        response = {"orderId": "short1", "side": "SELL_OPEN", "status": "FILLED",
                    "origQty": "1", "executedQty": "1", "avgPrice": "50000", "type": "MARKET"}
        client = _make_client(signed_response=response)
        await client.open_short(symbol="ETHUSDT", quantity=Decimal("0.5"))
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "ETH-SWAP-USDT"
        assert call_params["side"] == "SELL_OPEN"

    @pytest.mark.asyncio
    async def test_close_long_uses_sell_close(self) -> None:
        response = {"orderId": "cl1", "side": "SELL_CLOSE", "status": "FILLED",
                    "origQty": "0.5", "executedQty": "0.5", "avgPrice": "51000", "type": "MARKET"}
        client = _make_client(signed_response=response)
        await client.close_long(symbol="BTCUSDT", quantity=Decimal("0.5"))
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["side"] == "SELL_CLOSE"

    @pytest.mark.asyncio
    async def test_set_leverage(self) -> None:
        client = _make_client(signed_response={"leverage": "20", "symbol": "BTC-SWAP-USDT"})
        await client.set_leverage("BTCUSDT", 20)
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "BTC-SWAP-USDT"
        assert call_params["leverage"] == 20

    @pytest.mark.asyncio
    async def test_cancel_order(self) -> None:
        client = _make_client(signed_response={"status": "CANCELED"})
        await client.cancel_order(symbol="BTCUSDT", order_id="ord1")
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "BTC-SWAP-USDT"
        assert call_params["orderId"] == "ord1"

    @pytest.mark.asyncio
    async def test_get_mark_price(self) -> None:
        response = {
            "exchangeId": 301,
            "symbolId": "BTC-SWAP-USDT",
            "price": "62500.0",
            "time": 1000,
        }
        client = _make_client(public_response=response)
        price = await client.get_mark_price("BTCUSDT")
        assert price == Decimal("62500.0")

    @pytest.mark.asyncio
    async def test_create_listen_key(self) -> None:
        client = _make_client(signed_response={"listenKey": "abc123token"})
        key = await client.create_listen_key()
        assert key == "abc123token"

    @pytest.mark.asyncio
    async def test_today_pnl(self) -> None:
        response = {"code": 200, "data": {"dayProfit": "15.5", "dayProfitRate": "0.015"}}
        client = _make_client(signed_response=response)
        result = await client.get_today_pnl()
        assert result["data"]["dayProfit"] == "15.5"
