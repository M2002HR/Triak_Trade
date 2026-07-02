"""Tests for corrected Toobit futures client."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from triak_trade.exchange.toobit.errors import ToobitAPIError
from triak_trade.exchange.toobit.futures import (
    FuturesAccountInfo,
    FuturesBalance,
    FuturesContractSpec,
    FuturesOrder,
    FuturesPosition,
    ToobitFuturesClient,
    _dec,
    _demo_private_context_params,
    _fmt_decimal,
    _int,
    _normalize_limit_price,
    _normalize_stop_loss_price,
    _normalize_take_profit_price,
    _to_exchange_contract_quantity,
    from_futures_symbol,
    to_demo_futures_symbol,
    to_exchange_futures_symbol,
    to_futures_symbol,
)


def _make_client(signed_response: Any = None, public_response: Any = None) -> ToobitFuturesClient:
    mock_base = MagicMock()
    mock_base.signed_request = AsyncMock(return_value=signed_response or {})
    mock_base.public_request = AsyncMock(return_value=public_response or {})
    mock_base.get_exchange_info = AsyncMock(
        return_value={
            "contracts": [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "status": "TRADING",
                    "apiStatus": "TRADING",
                    "contractMultiplier": "0.001",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.1",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.0001",
                            "maxQty": "120",
                            "stepSize": "0.0001",
                        }
                    ],
                },
                {
                    "symbol": "ETH-SWAP-USDT",
                    "status": "TRADING",
                    "apiStatus": "TRADING",
                    "contractMultiplier": "0.01",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "tickSize": "0.01",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.01",
                            "maxQty": "10000",
                            "stepSize": "0.01",
                        }
                    ],
                },
            ]
        }
    )
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

    def test_to_demo_futures_symbol(self) -> None:
        assert to_demo_futures_symbol("BTCUSDT") == "TBV_BTC-SWAP-TBV_USDT"

    def test_from_demo_futures_symbol(self) -> None:
        assert from_futures_symbol("TBV_BTC-SWAP-TBV_USDT") == "BTCUSDT"

    def test_to_exchange_futures_symbol_demo(self) -> None:
        assert (
            to_exchange_futures_symbol("ETHUSDT", use_demo_symbol=True)
            == "TBV_ETH-SWAP-TBV_USDT"
        )


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

    def test_demo_private_context_params(self) -> None:
        assert _demo_private_context_params(True) == {"business_type": "VIRTUAL"}
        assert _demo_private_context_params(False) == {}

    def test_to_exchange_contract_quantity_uses_contract_multiplier(self) -> None:
        spec = FuturesContractSpec(
            {
                "symbol": "BTC-SWAP-USDT",
                "status": "TRADING",
                "apiStatus": "TRADING",
                "contractMultiplier": "0.001",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.0001",
                        "maxQty": "120",
                        "stepSize": "0.0001",
                    }
                ],
            }
        )
        assert _to_exchange_contract_quantity(Decimal("0.00230942"), spec) == Decimal("2.3")

    def test_to_exchange_contract_quantity_clamps_to_min_contracts(self) -> None:
        spec = FuturesContractSpec(
            {
                "symbol": "BTC-SWAP-USDT",
                "status": "TRADING",
                "apiStatus": "TRADING",
                "contractMultiplier": "0.001",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.0001",
                        "maxQty": "120",
                        "stepSize": "0.0001",
                    }
                ],
            }
        )
        assert _to_exchange_contract_quantity(Decimal("0.00001"), spec) == Decimal("0.1")

    def test_fmt_decimal_strips_trailing_zeroes(self) -> None:
        assert _fmt_decimal(Decimal("2.3000")) == "2.3"
        assert _fmt_decimal(Decimal("1.0000")) == "1"

    def test_normalize_stop_loss_price_uses_tick_size_and_safe_rounding(self) -> None:
        spec = FuturesContractSpec(
            {
                "symbol": "DOGE-SWAP-USDT",
                "status": "TRADING",
                "apiStatus": "TRADING",
                "contractMultiplier": "1",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.00001"},
                ],
            }
        )
        assert (
            _normalize_stop_loss_price(
                Decimal("0.0765659"),
                spec,
                position_side="SHORT",
            )
            == Decimal("0.07656")
        )
        assert (
            _normalize_stop_loss_price(
                Decimal("0.0765651"),
                spec,
                position_side="LONG",
            )
            == Decimal("0.07657")
        )

    def test_normalize_take_profit_and_limit_price_use_side_safe_rounding(self) -> None:
        spec = FuturesContractSpec(
            {
                "symbol": "BTC-SWAP-USDT",
                "status": "TRADING",
                "apiStatus": "TRADING",
                "contractMultiplier": "1",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                ],
            }
        )
        assert (
            _normalize_take_profit_price(
                Decimal("59999.99"),
                spec,
                position_side="SHORT",
            )
            == Decimal("60000.0")
        )
        assert _normalize_limit_price(Decimal("59999.91"), spec, side="SELL_CLOSE") == Decimal(
            "59999.9"
        )
        assert _normalize_limit_price(Decimal("59999.91"), spec, side="BUY_CLOSE") == Decimal(
            "60000.0"
        )

    def test_contract_spec_max_allowed_leverage_uses_risk_limit_tiers(self) -> None:
        spec = FuturesContractSpec(
            {
                "symbol": "DOGE-SWAP-USDT",
                "status": "TRADING",
                "apiStatus": "TRADING",
                "contractMultiplier": "1",
                "riskLimits": [
                    {"quantity": "428848", "value": "31366", "maxLeverage": "100"},
                    {"quantity": "1795802", "value": "131345", "maxLeverage": "50"},
                    {"quantity": "21442439", "value": "1568300", "maxLeverage": "25"},
                    {"quantity": "42884878", "value": "3136600", "maxLeverage": "20"},
                ],
            }
        )
        assert (
            spec.max_allowed_leverage(
                quantity=Decimal("1000000"),
                entry_price=Decimal("0.073"),
            )
            == 50
        )
        assert (
            spec.max_allowed_leverage(
                quantity=Decimal("25000000"),
                entry_price=Decimal("0.073"),
            )
            == 20
        )


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
        client._client.signed_request.assert_called_once_with(
            "GET",
            "/api/v1/futures/balance",
            params={},
        )

    @pytest.mark.asyncio
    async def test_get_futures_balance_demo_uses_virtual_business_type(self) -> None:
        client = _make_client(signed_response=[])
        await client.get_futures_balance(use_demo_account=True)
        client._client.signed_request.assert_called_once_with(
            "GET",
            "/api/v1/futures/balance",
            params={"business_type": "VIRTUAL"},
        )

    @pytest.mark.asyncio
    async def test_get_positions_empty(self) -> None:
        client = _make_client(signed_response=[])
        positions = await client.get_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_get_positions_demo_without_symbol_uses_virtual_business_type(self) -> None:
        client = _make_client(signed_response=[])
        await client.get_positions(use_demo_symbol=True)
        client._client.signed_request.assert_called_once_with(
            "GET",
            "/api/v1/futures/positions",
            params={"business_type": "VIRTUAL"},
        )

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
    async def test_validate_symbol_tradable(self) -> None:
        exchange_info = {
            "contracts": [
                {
                    "symbol": "BTC-SWAP-USDT",
                    "status": "TRADING",
                    "apiStatus": "TRADING",
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100"},
                    ],
                }
            ]
        }
        client = _make_client()
        client._client.get_exchange_info = AsyncMock(return_value=exchange_info)
        spec = await client.validate_symbol_tradable("BTCUSDT")
        assert isinstance(spec, FuturesContractSpec)
        assert spec.symbol_internal == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_validate_symbol_tradable_rejects_unknown(self) -> None:
        client = _make_client()
        client._client.get_exchange_info = AsyncMock(return_value={"contracts": []})
        with pytest.raises(ToobitAPIError):
            await client.validate_symbol_tradable("VETUSDT")

    @pytest.mark.asyncio
    async def test_validate_symbol_tradable_probes_demo_symbol(self) -> None:
        response = {
            "exchangeId": 301,
            "symbolId": "TBV_BTC-SWAP-TBV_USDT",
            "price": "62500.0",
            "time": 1000,
        }
        client = _make_client(public_response=response)
        spec = await client.validate_symbol_tradable("BTCUSDT", use_demo_symbol=True)
        assert spec.symbol == "TBV_BTC-SWAP-TBV_USDT"
        client._client.public_request.assert_called_once_with(
            "GET",
            "/quote/v1/markPrice",
            params={"symbol": "TBV_BTC-SWAP-TBV_USDT"},
        )

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
        assert params["type"] == "LIMIT"
        assert params["priceType"] == "MARKET"
        assert params["quantity"] == "1000"

    @pytest.mark.asyncio
    async def test_place_order_limit_normalizes_price_to_tick_size(self) -> None:
        response = {"orderId": "ord_limit", "side": "SELL_CLOSE", "type": "LIMIT",
                    "origQty": "1", "executedQty": "0", "avgPrice": "0", "status": "NEW"}
        client = _make_client(signed_response=response)
        await client.place_order(
            symbol="BTCUSDT",
            side="SELL_CLOSE",
            order_type="LIMIT",
            quantity=Decimal("0.01"),
            price=Decimal("59999.99"),
        )
        params = client._client.signed_request.call_args[1]["params"]
        assert params["price"] == "59999.9"

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
        assert call_params["quantity"] == "50"

    @pytest.mark.asyncio
    async def test_open_long_demo_uses_tbv_symbol(self) -> None:
        response = {"orderId": "demo1", "side": "BUY_OPEN", "status": "FILLED",
                    "origQty": "1", "executedQty": "1", "avgPrice": "50000", "type": "MARKET",
                    "symbol": "TBV_BTC-SWAP-TBV_USDT"}
        client = _make_client(signed_response=response)
        order = await client.open_long(
            symbol="BTCUSDT",
            quantity=Decimal("1"),
            leverage=10,
            use_demo_symbol=True,
        )
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert call_params["business_type"] == "VIRTUAL"
        assert order.symbol_internal == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_place_order_demo_uses_virtual_business_type(self) -> None:
        client = _make_client()
        client._client.signed_request = AsyncMock(
            return_value={
                "orderId": "demo1",
                "symbol": "TBV_BTC-SWAP-TBV_USDT",
                "side": "BUY_OPEN",
                "status": "FILLED",
                "type": "MARKET",
            }
        )
        await client.open_long(
            symbol="BTCUSDT",
            quantity=Decimal("0.00230942"),
            use_demo_symbol=True,
        )
        call_params = client._client.signed_request.await_args.kwargs["params"]
        assert call_params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert call_params["business_type"] == "VIRTUAL"

    @pytest.mark.asyncio
    async def test_place_order_demo_rewrites_private_endpoint_error_in_tbv_only_mode(self) -> None:
        client = _make_client()
        client.demo_private_symbol_mode = "tbv_only"
        client._client.signed_request = AsyncMock(
            side_effect=[
                ToobitAPIError(
                    "Toobit API HTTP error: 400: invalid",
                    status_code=400,
                    error_code=-1130,
                    payload={"code": -1130, "msg": "Data sent for paramter '%s' is not valid."},
                ),
                ToobitAPIError(
                    "Toobit API HTTP error: 400: invalid",
                    status_code=400,
                    error_code=-1130,
                    payload={"code": -1130, "msg": "Data sent for paramter '%s' is not valid."},
                ),
            ]
        )
        with pytest.raises(ToobitAPIError, match="business_type=VIRTUAL"):
            await client.open_long(
                symbol="BTCUSDT",
                quantity=Decimal("0.00230942"),
                use_demo_symbol=True,
            )
        assert client._client.signed_request.await_count == 2

    @pytest.mark.asyncio
    async def test_place_order_demo_retries_with_live_symbol_after_tbv_invalid_param(self) -> None:
        client = _make_client()
        client._client.signed_request = AsyncMock(
            side_effect=[
                ToobitAPIError(
                    "Toobit API HTTP error: 400: invalid",
                    status_code=400,
                    error_code=-1130,
                    payload={"code": -1130, "msg": "Data sent for paramter '%s' is not valid."},
                ),
                {
                    "orderId": "demo_retry_ok",
                    "symbol": "BTC-SWAP-USDT",
                    "side": "SELL_CLOSE",
                    "status": "NEW",
                    "type": "LIMIT",
                },
            ]
        )

        order = await client.place_order(
            symbol="BTCUSDT",
            side="SELL_CLOSE",
            order_type="LIMIT",
            quantity=Decimal("0.001"),
            price=Decimal("59999.9"),
            use_demo_symbol=True,
        )

        first_params = client._client.signed_request.await_args_list[0].kwargs["params"]
        second_params = client._client.signed_request.await_args_list[1].kwargs["params"]
        assert first_params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert second_params["symbol"] == "BTC-SWAP-USDT"
        assert first_params["business_type"] == "VIRTUAL"
        assert second_params["business_type"] == "VIRTUAL"
        assert order.order_id == "demo_retry_ok"

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
    async def test_set_leverage_demo_uses_virtual_business_type(self) -> None:
        client = _make_client(signed_response={"leverage": "20", "symbol": "TBV_BTC-SWAP-TBV_USDT"})
        await client.set_leverage("BTCUSDT", 20, use_demo_symbol=True)
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert call_params["business_type"] == "VIRTUAL"

    @pytest.mark.asyncio
    async def test_set_trading_stop_uses_position_trading_stop_endpoint(self) -> None:
        client = _make_client(
            signed_response={
                "symbol": "BTC-SWAP-USDT",
                "side": "LONG",
                "takeProfit": "51000",
                "stopLoss": "49000",
                "tpSize": "3.5",
                "slSize": "10",
            }
        )
        response = await client.set_trading_stop(
            symbol="BTCUSDT",
            side="LONG",
            take_profit=Decimal("51000"),
            stop_loss=Decimal("49000"),
            tp_quantity=Decimal("0.0035"),
            sl_quantity=Decimal("0.01"),
        )
        assert response["takeProfit"] == "51000"
        client._client.signed_request.assert_called_once()
        call = client._client.signed_request.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == "/api/v1/futures/position/trading-stop"
        params = call.kwargs["params"]
        assert params["symbol"] == "BTC-SWAP-USDT"
        assert params["side"] == "LONG"
        assert params["takeProfit"] == "51000"
        assert params["stopLoss"] == "49000"
        assert params["tpSize"] == "3"
        assert params["slSize"] == "10"

    @pytest.mark.asyncio
    async def test_set_trading_stop_normalizes_prices_to_contract_tick_size(self) -> None:
        client = _make_client(signed_response={"ok": True})
        await client.set_trading_stop(
            symbol="BTCUSDT",
            side="SHORT",
            take_profit=Decimal("59999.99"),
            stop_loss=Decimal("62340.69"),
            sl_quantity=Decimal("0.01"),
        )
        params = client._client.signed_request.call_args[1]["params"]
        assert params["takeProfit"] == "60000"
        assert params["stopLoss"] == "62340.6"

    @pytest.mark.asyncio
    async def test_normalize_trade_protection_returns_exchange_aligned_prices(self) -> None:
        client = _make_client()
        stop_loss, take_profits = await client.normalize_trade_protection(
            symbol="BTCUSDT",
            side="SHORT",
            stop_loss=Decimal("62340.69"),
            take_profits=[Decimal("59999.99"), Decimal("59888.81")],
        )
        assert stop_loss == Decimal("62340.6")
        assert take_profits == [Decimal("60000.0"), Decimal("59888.9")]

    @pytest.mark.asyncio
    async def test_get_open_algo_orders_uses_v2_stop_profit_loss_endpoint(self) -> None:
        client = _make_client(
            signed_response={
                "code": 200,
                "msg": "success",
                "data": [
                    {
                        "orderId": "tp1",
                        "symbol": "TBV_BTC-SWAP-TBV_USDT",
                        "orderType": "STOP_LONG_PROFIT",
                        "positionSide": "LONG",
                        "stopPrice": "60500",
                        "status": "NEW",
                    }
                ],
            }
        )
        orders = await client.get_open_algo_orders("BTCUSDT", use_demo_symbol=True)
        assert len(orders) == 1
        assert orders[0].order_type == "STOP_LONG_PROFIT"
        call = client._client.signed_request.call_args
        assert call.args[0] == "GET"
        assert call.args[1] == "/api/v2/futures/open-algo-orders"
        params = call.kwargs["params"]
        assert params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert params["stopCategory"] == "STOP_PROFIT_LOSS"
        assert params["business_type"] == "VIRTUAL"

    @pytest.mark.asyncio
    async def test_cancel_algo_order_uses_v2_algo_order_endpoint(self) -> None:
        client = _make_client(signed_response={"code": 200, "msg": "success", "data": None})
        payload = await client.cancel_algo_order("12345")
        assert payload["code"] == 200
        call = client._client.signed_request.call_args
        assert call.args[0] == "DELETE"
        assert call.args[1] == "/api/v2/futures/algo-order"
        assert call.kwargs["params"]["orderId"] == "12345"

    @pytest.mark.asyncio
    async def test_cancel_order(self) -> None:
        client = _make_client(signed_response={"status": "CANCELED"})
        await client.cancel_order(symbol="BTCUSDT", order_id="ord1", order_type="STOP")
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "BTC-SWAP-USDT"
        assert call_params["orderId"] == "ord1"
        assert call_params["type"] == "STOP"

    @pytest.mark.asyncio
    async def test_get_open_orders_can_query_stop_profit_loss_orders(self) -> None:
        client = _make_client(
            signed_response=[
                {
                    "orderId": "tp1",
                    "symbol": "TBV_BTC-SWAP-TBV_USDT",
                    "type": "STOP_PROFIT_LOSS",
                    "orderType": "STOP_LONG_PROFIT",
                    "positionSide": "LONG",
                    "stopPrice": "60500",
                    "status": "ORDER_NEW",
                }
            ]
        )
        orders = await client.get_open_orders(
            "BTCUSDT",
            order_type="STOP_PROFIT_LOSS",
            use_demo_symbol=True,
        )
        assert len(orders) == 1
        assert orders[0].order_type == "STOP_LONG_PROFIT"
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert call_params["type"] == "STOP_PROFIT_LOSS"
        assert call_params["business_type"] == "VIRTUAL"

    @pytest.mark.asyncio
    async def test_get_order_queries_single_stop_order(self) -> None:
        client = _make_client(
            signed_response={
                "orderId": "sl1",
                "symbol": "TBV_BTC-SWAP-TBV_USDT",
                "type": "STOP_PROFIT_LOSS",
                "orderType": "STOP_SHORT_LOSS",
                "executedOrderId": "close_1",
                "stopPrice": "75000",
                "status": "ORDER_FILLED",
            }
        )
        order = await client.get_order(
            symbol="BTCUSDT",
            order_id="sl1",
            order_type="STOP",
            use_demo_symbol=True,
        )
        assert order.order_id == "sl1"
        assert order.executed_order_id == "close_1"
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert call_params["type"] == "STOP"
        assert call_params["orderId"] == "sl1"

    @pytest.mark.asyncio
    async def test_get_user_trades_demo_uses_virtual_business_type(self) -> None:
        client = _make_client(
            signed_response=[
                {
                    "id": "1",
                    "orderId": "ord1",
                    "symbol": "TBV_BTC-SWAP-TBV_USDT",
                    "price": "50000",
                    "qty": "1",
                    "commissionAsset": "TBV_USDT",
                    "commission": "0.1",
                    "type": "MARKET",
                    "side": "BUY_CLOSE",
                    "realizedPnl": "2.5",
                }
            ]
        )
        trades = await client.get_user_trades("BTCUSDT", use_demo_symbol=True)
        call_params = client._client.signed_request.call_args[1]["params"]
        assert call_params["symbol"] == "TBV_BTC-SWAP-TBV_USDT"
        assert call_params["business_type"] == "VIRTUAL"
        assert trades[0].realized_pnl == Decimal("2.5")

    @pytest.mark.asyncio
    async def test_wait_for_order_fill_returns_history_and_matching_fills(self) -> None:
        client = _make_client()
        client.get_order_history = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                FuturesOrder(
                    {
                        "orderId": "ord1",
                        "symbol": "BTC-SWAP-USDT",
                        "status": "FILLED",
                        "executedQty": "10",
                        "avgPrice": "50010",
                    }
                )
            ]
        )
        client.get_user_trades = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                type(
                    "Fill",
                    (),
                    {
                        "order_id": "ord1",
                        "qty": Decimal("10"),
                        "realized_pnl": Decimal("3.2"),
                        "commission": Decimal("0.4"),
                        "price": Decimal("50010"),
                    },
                )()
            ]
        )
        order, fills = await client.wait_for_order_fill(symbol="BTCUSDT", order_id="ord1")
        assert order is not None
        assert order.order_id == "ord1"
        assert len(fills) == 1
        assert fills[0].realized_pnl == Decimal("3.2")

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
    async def test_get_mark_price_demo_uses_tbv_symbol(self) -> None:
        response = {
            "exchangeId": 301,
            "symbolId": "TBV_BTC-SWAP-TBV_USDT",
            "price": "62500.0",
            "time": 1000,
        }
        client = _make_client(public_response=response)
        price = await client.get_mark_price("BTCUSDT", use_demo_symbol=True)
        assert price == Decimal("62500.0")
        client._client.public_request.assert_called_once_with(
            "GET",
            "/quote/v1/markPrice",
            params={"symbol": "TBV_BTC-SWAP-TBV_USDT"},
        )

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
