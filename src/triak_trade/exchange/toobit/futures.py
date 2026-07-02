"""Toobit futures trading client — corrected endpoints verified against live API."""

from __future__ import annotations

import asyncio
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.errors import ToobitAPIError

# ── Symbol Helpers ─────────────────────────────────────────────────────────────

DEMO_SYMBOL_PREFIX = "TBV_"
DEMO_QUOTE_PREFIX = "TBV_"

def to_futures_symbol(symbol: str) -> str:
    """Convert BTCUSDT → BTC-SWAP-USDT for Toobit futures API."""
    s = symbol.strip().upper()
    if "-SWAP-" in s:
        return s  # already in correct format
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}-SWAP-USDT"
    if s.endswith("USDC") and len(s) > 4:
        return f"{s[:-4]}-SWAP-USDC"
    return s


def from_futures_symbol(symbol: str) -> str:
    """Convert BTC-SWAP-USDT → BTCUSDT for internal use."""
    s = symbol.strip().upper()
    if s.startswith(DEMO_SYMBOL_PREFIX) and "-SWAP-" in s:
        base, _, quote = s.partition("-SWAP-")
        base = base.removeprefix(DEMO_SYMBOL_PREFIX)
        quote = quote.removeprefix(DEMO_QUOTE_PREFIX)
        return f"{base}{quote.replace('-', '')}"
    if "-SWAP-USDT" in s:
        return s.replace("-SWAP-USDT", "USDT")
    if "-SWAP-USDC" in s:
        return s.replace("-SWAP-USDC", "USDC")
    return s


def to_demo_futures_symbol(symbol: str) -> str:
    """Convert BTCUSDT → TBV_BTC-SWAP-TBV_USDT for Toobit demo futures."""
    s = symbol.strip().upper()
    if s.startswith(DEMO_SYMBOL_PREFIX) and "-SWAP-" in s:
        return s
    contract = to_futures_symbol(s)
    if "-SWAP-" not in contract:
        return f"{DEMO_SYMBOL_PREFIX}{contract}"
    base, _, quote = contract.partition("-SWAP-")
    return f"{DEMO_SYMBOL_PREFIX}{base}-SWAP-{DEMO_QUOTE_PREFIX}{quote}"


def to_exchange_futures_symbol(symbol: str, *, use_demo_symbol: bool = False) -> str:
    if use_demo_symbol:
        return to_demo_futures_symbol(symbol)
    return to_futures_symbol(symbol)


# ── Response Models ────────────────────────────────────────────────────────────

class FuturesBalance:
    """Single asset balance from /api/v1/futures/balance."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.coin: str = str(raw.get("coin", "USDT"))
        self.asset: str = str(raw.get("asset", self.coin))
        self.balance: Decimal = _dec(raw.get("balance", "0"))
        self.available_balance: Decimal = _dec(raw.get("availableBalance", "0"))
        self.position_margin: Decimal = _dec(raw.get("positionMargin", "0"))
        self.order_margin: Decimal = _dec(raw.get("orderMargin", "0"))
        self.cross_unrealized_pnl: Decimal = _dec(raw.get("crossUnRealizedPnl", "0"))
        self.coupon: Decimal = _dec(raw.get("coupon", "0"))
        self.raw: dict[str, Any] = raw


class SpotBalance:
    """Single asset balance from /api/v1/account balances list."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.asset: str = str(raw.get("asset", ""))
        self.asset_name: str = str(raw.get("assetName", self.asset))
        self.total: Decimal = _dec(raw.get("total", "0"))
        self.free: Decimal = _dec(raw.get("free", "0"))
        self.locked: Decimal = _dec(raw.get("locked", "0"))


class SpotAccountInfo:
    """Account info from GET /api/v1/account."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.user_id: str = str(raw.get("userId", ""))
        balances_raw = raw.get("balances", [])
        self.balances: list[SpotBalance] = [
            SpotBalance(b) for b in (balances_raw if isinstance(balances_raw, list) else [])
        ]

    def usdt_balance(self) -> SpotBalance | None:
        for b in self.balances:
            asset = b.asset.upper()
            if asset == "USDT" or asset.endswith("_USDT"):
                return b
        return None

    def nonzero_balances(self) -> list[SpotBalance]:
        return [b for b in self.balances if b.total > 0]


class FuturesAccountInfo:
    """Aggregated futures account info."""

    def __init__(
        self,
        balances: list[FuturesBalance],
        today_pnl: dict[str, Any] | None = None,
        api_key_type: str = "",
        user_id: str = "",
    ) -> None:
        self.balances = balances
        self.user_id = user_id
        self.api_key_type = api_key_type

        pnl_data = (today_pnl or {}).get("data", today_pnl or {})
        self.day_profit: Decimal = _dec(pnl_data.get("dayProfit", "0"))
        self.day_profit_rate: Decimal = _dec(pnl_data.get("dayProfitRate", "0"))

    def usdt_balance(self) -> FuturesBalance | None:
        for b in self.balances:
            coin = b.coin.upper()
            if coin == "USDT" or coin.endswith("_USDT"):
                return b
        return self.balances[0] if self.balances else None

    @property
    def total_wallet_balance(self) -> Decimal:
        b = self.usdt_balance()
        return b.balance if b else Decimal("0")

    @property
    def available_balance(self) -> Decimal:
        b = self.usdt_balance()
        return b.available_balance if b else Decimal("0")

    @property
    def total_unrealized_profit(self) -> Decimal:
        b = self.usdt_balance()
        return b.cross_unrealized_pnl if b else Decimal("0")

    @property
    def total_position_margin(self) -> Decimal:
        b = self.usdt_balance()
        return b.position_margin if b else Decimal("0")


class FuturesPosition:
    """Open futures position from /api/v1/futures/positions."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.symbol: str = str(raw.get("symbol", ""))
        self.exchange_symbol: str = self.symbol
        self.symbol_internal: str = from_futures_symbol(self.symbol)
        self.side: str = str(raw.get("side", ""))           # LONG or SHORT
        self.avg_price: Decimal = _dec(raw.get("avgPrice", "0"))
        self.position: Decimal = _dec(raw.get("position", "0"))   # qty in contracts
        self.available: Decimal = _dec(raw.get("available", "0"))
        self.leverage: int = _int(raw.get("leverage", "1"))
        self.last_price: Decimal = _dec(raw.get("lastPrice", "0"))
        self.position_value: Decimal = _dec(raw.get("positionValue", "0"))
        self.margin: Decimal = _dec(raw.get("margin", "0"))
        self.margin_rate: Decimal = _dec(raw.get("marginRate", "0"))
        self.unrealized_pnl: Decimal = _dec(raw.get("unrealizedPnL", "0"))
        self.profit_rate: Decimal = _dec(raw.get("profitRate", "0"))
        self.realized_pnl: Decimal = _dec(raw.get("realizedPnL", "0"))
        self.mark_price: Decimal = _dec(raw.get("markPrice", "0"))
        self.liquidation_price: Decimal = _dec(raw.get("flp", "0"))
        self.margin_type: str = str(raw.get("marginType", "CROSS"))
        self.raw: dict[str, Any] = raw

    @property
    def is_long(self) -> bool:
        return self.side.upper() == "LONG"

    @property
    def is_open(self) -> bool:
        return abs(self.position) > Decimal("0")


class FuturesOrder:
    """Futures order response."""

    def __init__(self, raw: dict[str, Any]) -> None:
        data = raw.get("data", raw) if "data" in raw else raw
        self.order_id: str = str(data.get("orderId", ""))
        self.client_order_id: str = str(data.get("clientOrderId", ""))
        self.symbol: str = str(data.get("symbol", ""))
        self.exchange_symbol: str = self.symbol
        self.symbol_internal: str = from_futures_symbol(self.symbol)
        self.side: str = str(data.get("side", ""))
        self.order_type: str = str(data.get("orderType", ""))
        if not self.order_type:
            self.order_type = str(data.get("type", ""))
        self.orig_qty: Decimal = _dec(data.get("origQty", "0"))
        self.executed_qty: Decimal = _dec(data.get("executedQty", "0"))
        self.price: Decimal = _dec(data.get("price", "0"))
        self.avg_price: Decimal = _dec(data.get("avgPrice", "0"))
        self.status: str = str(data.get("status", ""))
        self.leverage: int = _int(data.get("leverage", "1"))
        self.position_side: str = str(data.get("positionSide", ""))
        self.time_in_force: str = str(data.get("timeInForce", "GTC"))
        self.margin_locked: Decimal = _dec(data.get("marginLocked", "0"))
        self.executed_order_id: str = str(data.get("executedOrderId", ""))
        self.stop_price: Decimal = _dec(data.get("stopPrice", "0"))
        self.trigger_by: str = str(data.get("triggerBy", ""))
        self.price_type: str = str(data.get("priceType", ""))
        self.trigger_type: int = _int(data.get("triggerType", "0"))
        self.active_status: int = _int(data.get("activeStatus", "0"))
        self.stop_type: str = str(data.get("stopType", ""))
        self.raw: dict[str, Any] = data


class FuturesUserTrade:
    """Filled futures trade row from /api/v1/futures/userTrades."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.time: int = _int(raw.get("time", "0"))
        self.trade_id: str = str(raw.get("id", ""))
        self.order_id: str = str(raw.get("orderId", ""))
        self.symbol: str = str(raw.get("symbol", ""))
        self.exchange_symbol: str = self.symbol
        self.symbol_internal: str = from_futures_symbol(self.symbol)
        self.price: Decimal = _dec(raw.get("price", "0"))
        self.qty: Decimal = _dec(raw.get("qty", "0"))
        self.commission_asset: str = str(raw.get("commissionAsset", ""))
        self.commission: Decimal = _dec(raw.get("commission", "0"))
        self.maker_rebate: Decimal = _dec(raw.get("makerRebate", "0"))
        self.order_type: str = str(raw.get("type", ""))
        self.side: str = str(raw.get("side", ""))
        self.realized_pnl: Decimal = _dec(raw.get("realizedPnl", "0"))
        self.ticket_id: str = str(raw.get("ticketId", ""))
        self.is_maker: bool = bool(raw.get("isMaker", False))
        self.raw: dict[str, Any] = raw


class FuturesRiskLimit:
    """Per-tier leverage/risk bracket from futures exchangeInfo."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.risk_limit_id: str = str(raw.get("riskLimitId", ""))
        self.quantity: Decimal = _dec(raw.get("quantity", "0"))
        self.value: Decimal = _dec(raw.get("value", "0"))
        self.initial_margin: Decimal = _dec(raw.get("initialMargin", "0"))
        self.maint_margin: Decimal = _dec(raw.get("maintMargin", "0"))
        self.max_leverage: int = _int(raw.get("maxLeverage", "0"))
        self.raw: dict[str, Any] = raw


class FuturesContractSpec:
    """Tradable futures contract metadata from exchangeInfo."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.symbol: str = str(raw.get("symbol", ""))
        self.exchange_symbol: str = self.symbol
        self.symbol_internal: str = from_futures_symbol(self.symbol)
        self.status: str = str(raw.get("status", "UNKNOWN"))
        self.api_status: str = str(raw.get("apiStatus", self.status or "UNKNOWN"))
        self.min_qty: Decimal = Decimal("0")
        self.max_qty: Decimal = Decimal("0")
        self.tick_size: Decimal = Decimal("0")
        self.step_size: Decimal = Decimal("0")
        self.contract_multiplier: Decimal = _dec(raw.get("contractMultiplier", "1"))
        risk_limits = raw.get("riskLimits", [])
        self.risk_limits: list[FuturesRiskLimit] = sorted(
            [
                FuturesRiskLimit(item)
                for item in risk_limits
                if isinstance(item, dict)
            ],
            key=lambda item: (item.quantity, item.value, item.max_leverage),
        )
        self.raw: dict[str, Any] = raw
        filters = raw.get("filters", [])
        if isinstance(filters, list):
            for item in filters:
                if not isinstance(item, dict):
                    continue
                if item.get("filterType") == "LOT_SIZE":
                    self.min_qty = _dec(item.get("minQty", "0"))
                    self.max_qty = _dec(item.get("maxQty", "0"))
                    self.step_size = _dec(item.get("stepSize", "0"))
                if item.get("filterType") == "PRICE_FILTER":
                    self.tick_size = _dec(item.get("tickSize", "0"))

    @property
    def is_tradable(self) -> bool:
        return self.status == "TRADING" and self.api_status not in {
            "API_TRADE_FORBIDDEN",
            "OPEN_FORBIDDEN",
            "CLOSE_FORBIDDEN",
        }

    @property
    def contract_min_qty(self) -> Decimal:
        if self.contract_multiplier <= 0:
            return self.min_qty
        return self.min_qty / self.contract_multiplier

    @property
    def contract_max_qty(self) -> Decimal:
        if self.contract_multiplier <= 0:
            return self.max_qty
        return self.max_qty / self.contract_multiplier

    @property
    def contract_step_size(self) -> Decimal:
        if self.contract_multiplier <= 0:
            return self.step_size
        return self.step_size / self.contract_multiplier

    def to_exchange_contract_quantity(self, quantity: Decimal) -> Decimal:
        return _to_exchange_contract_quantity(quantity, self)

    def from_exchange_contract_quantity(self, quantity: Decimal) -> Decimal:
        return _from_exchange_contract_quantity(quantity, self)

    def max_allowed_leverage(
        self,
        *,
        quantity: Decimal,
        entry_price: Decimal,
    ) -> int | None:
        if quantity <= 0 or entry_price <= 0 or not self.risk_limits:
            return None
        exchange_quantity = (
            quantity
            if self.contract_multiplier <= 0
            else (quantity / self.contract_multiplier)
        )
        notional_value = quantity * entry_price
        for limit in self.risk_limits:
            quantity_ok = limit.quantity <= 0 or exchange_quantity <= limit.quantity
            value_ok = limit.value <= 0 or notional_value <= limit.value
            if quantity_ok and value_ok:
                return limit.max_leverage if limit.max_leverage > 0 else None
        last = self.risk_limits[-1]
        return last.max_leverage if last.max_leverage > 0 else None


# ── Main Client ────────────────────────────────────────────────────────────────

class ToobitFuturesClient:
    """
    Toobit futures trading client.
    All endpoints verified against live API at api.toobit.com.
    """

    def __init__(
        self,
        *,
        client: ToobitClient,
        demo_private_symbol_mode: str = "auto",
        # Futures endpoints
        futures_balance_path: str = "/api/v1/futures/balance",
        futures_positions_path: str = "/api/v1/futures/positions",
        futures_order_v2_path: str = "/api/v2/futures/order",
        futures_order_v1_path: str = "/api/v1/futures/order",
        futures_open_orders_path: str = "/api/v1/futures/openOrders",
        futures_leverage_path: str = "/api/v1/futures/leverage",
        futures_flash_close_path: str = "/api/v1/futures/flashClose",
        futures_position_trading_stop_path: str = "/api/v1/futures/position/trading-stop",
        futures_v2_open_algo_orders_path: str = "/api/v2/futures/open-algo-orders",
        futures_v2_algo_order_path: str = "/api/v2/futures/algo-order",
        futures_today_pnl_path: str = "/api/v1/futures/todayPnl",
        futures_user_trades_path: str = "/api/v1/futures/userTrades",
        futures_listen_key_path: str = "/api/v1/listenKey",
        futures_history_orders_path: str = "/api/v1/futures/historyOrders",
        # Spot endpoints
        spot_account_path: str = "/api/v1/account",
        spot_check_api_key_path: str = "/api/v1/account/checkApiKey",
        # Market data
        mark_price_path: str = "/quote/v1/markPrice",
        ticker_price_path: str = "/quote/v1/contract/ticker/price",
        ticker_24hr_path: str = "/quote/v1/contract/ticker/24hr",
    ) -> None:
        self._client = client
        self.demo_private_symbol_mode = demo_private_symbol_mode.strip().lower()
        self.futures_balance_path = futures_balance_path
        self.futures_positions_path = futures_positions_path
        self.futures_order_v2_path = futures_order_v2_path
        self.futures_order_v1_path = futures_order_v1_path
        self.futures_open_orders_path = futures_open_orders_path
        self.futures_leverage_path = futures_leverage_path
        self.futures_flash_close_path = futures_flash_close_path
        self.futures_position_trading_stop_path = futures_position_trading_stop_path
        self.futures_v2_open_algo_orders_path = futures_v2_open_algo_orders_path
        self.futures_v2_algo_order_path = futures_v2_algo_order_path
        self.futures_today_pnl_path = futures_today_pnl_path
        self.futures_user_trades_path = futures_user_trades_path
        self.futures_listen_key_path = futures_listen_key_path
        self.futures_history_orders_path = futures_history_orders_path
        self.spot_account_path = spot_account_path
        self.spot_check_api_key_path = spot_check_api_key_path
        self.mark_price_path = mark_price_path
        self.ticker_price_path = ticker_price_path
        self.ticker_24hr_path = ticker_24hr_path

    # ── Account Info ──────────────────────────────────────────────────────────

    async def get_futures_balance(
        self,
        *,
        use_demo_account: bool = False,
    ) -> list[FuturesBalance]:
        payload = await self._client.signed_request(
            "GET",
            self.futures_balance_path,
            params=_demo_private_context_params(use_demo_account),
        )
        if isinstance(payload, list):
            return [FuturesBalance(item) for item in payload]
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [FuturesBalance(item) for item in data]
        return []

    async def get_today_pnl(
        self,
        *,
        use_demo_account: bool = False,
    ) -> dict[str, Any]:
        payload = await self._client.signed_request(
            "GET",
            self.futures_today_pnl_path,
            params=_demo_private_context_params(use_demo_account),
        )
        return payload

    async def get_spot_account(self) -> SpotAccountInfo:
        payload = await self._client.signed_request("GET", self.spot_account_path)
        return SpotAccountInfo(payload if isinstance(payload, dict) else {})

    async def check_api_key(self) -> dict[str, Any]:
        return await self._client.signed_request("GET", self.spot_check_api_key_path)

    async def get_full_account_info(
        self,
        *,
        use_demo_account: bool = False,
    ) -> FuturesAccountInfo:
        """Fetch all account info in parallel (balance + PnL + key type)."""
        import asyncio
        balances_task = asyncio.create_task(
            self.get_futures_balance(use_demo_account=use_demo_account)
        )
        pnl_task = asyncio.create_task(
            self.get_today_pnl(use_demo_account=use_demo_account)
        )
        key_task = asyncio.create_task(self.check_api_key())
        spot_task = asyncio.create_task(self.get_spot_account())

        balances, pnl, key_info, spot = await asyncio.gather(
            balances_task, pnl_task, key_task, spot_task,
            return_exceptions=True,
        )

        if isinstance(balances, Exception):
            balances = []
        if isinstance(pnl, Exception):
            pnl = {}
        if isinstance(key_info, Exception):
            key_info = {}
        if isinstance(spot, Exception):
            spot = SpotAccountInfo({})

        return FuturesAccountInfo(
            balances=balances,
            today_pnl=pnl,
            api_key_type=key_info.get("accountType", "") if isinstance(key_info, dict) else "",
            user_id=spot.user_id if isinstance(spot, SpotAccountInfo) else "",
        )

    # ── Positions ─────────────────────────────────────────────────────────────

    async def get_positions(
        self,
        symbol: str | None = None,
        *,
        use_demo_symbol: bool = False,
    ) -> list[FuturesPosition]:
        params: dict[str, object] = {}
        if symbol:
            payload = await self._private_symbol_request(
                "GET",
                self.futures_positions_path,
                symbol=symbol,
                use_demo_symbol=use_demo_symbol,
                params=params,
            )
        else:
            payload = await self._client.signed_request(
                "GET",
                self.futures_positions_path,
                params=_demo_private_context_params(use_demo_symbol),
            )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesPosition(item) for item in (items if isinstance(items, list) else [])]

    async def get_open_positions(
        self,
        symbol: str | None = None,
        *,
        use_demo_symbol: bool = False,
    ) -> list[FuturesPosition]:
        return [
            p
            for p in await self.get_positions(
                symbol,
                use_demo_symbol=use_demo_symbol,
            )
            if p.is_open
        ]

    async def get_contract_specs(self) -> list[FuturesContractSpec]:
        payload = await self._client.get_exchange_info()
        contracts = payload.get("contracts", []) if isinstance(payload, dict) else []
        return [
            FuturesContractSpec(item) for item in contracts if isinstance(item, dict)
        ]

    async def get_contract_spec(
        self,
        symbol: str,
        *,
        use_demo_symbol: bool = False,
    ) -> FuturesContractSpec | None:
        requested = to_exchange_futures_symbol(symbol, use_demo_symbol=use_demo_symbol)
        for spec in await self.get_contract_specs():
            if spec.symbol == requested or spec.symbol_internal == from_futures_symbol(requested):
                return spec
        return None

    async def validate_symbol_tradable(
        self,
        symbol: str,
        *,
        use_demo_symbol: bool = False,
    ) -> FuturesContractSpec:
        if use_demo_symbol:
            exchange_symbol = to_exchange_futures_symbol(symbol, use_demo_symbol=True)
            try:
                await self.get_mark_price(symbol, use_demo_symbol=True)
            except Exception as exc:
                raise ToobitAPIError(
                    f"Demo symbol {exchange_symbol} is not available on Toobit demo futures"
                ) from exc
            return FuturesContractSpec(
                {
                    "symbol": exchange_symbol,
                    "status": "TRADING",
                    "apiStatus": "TRADING",
                    "filters": [],
                }
            )
        spec = await self.get_contract_spec(symbol)
        if spec is None:
            raise ToobitAPIError(f"Symbol {symbol} is not listed on Toobit futures")
        if not spec.is_tradable:
            raise ToobitAPIError(
                "Symbol "
                f"{symbol} is not tradable via API "
                f"(status={spec.status}, api_status={spec.api_status})"
            )
        return spec

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
        quantity: Decimal,
        price: Decimal | None = None,
        leverage: int | None = None,
        client_order_id: str | None = None,
        time_in_force: str = "GTC",
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        use_demo_symbol: bool = False,
    ) -> FuturesOrder:
        """Place a futures order using Toobit's v1 futures trade semantics."""
        spec = await self._require_order_spec(symbol)
        exchange_qty = _to_exchange_contract_quantity(quantity, spec)
        normalized_order_type = order_type.strip().upper()
        params: dict[str, object] = {
            "side": side.strip().upper(),
            "type": "LIMIT",
            "quantity": _fmt_decimal(exchange_qty),
            "newClientOrderId": client_order_id or f"triak_{int(time.time() * 1000)}",
        }
        if leverage is not None:
            params["leverage"] = str(leverage)
        if normalized_order_type == "LIMIT":
            params["priceType"] = "INPUT"
            if price is None:
                raise ToobitAPIError("price is required for LIMIT futures orders")
            params["price"] = _fmt_decimal(
                _normalize_limit_price(
                    price,
                    spec,
                    side=side,
                )
            )
            params["timeInForce"] = time_in_force
        elif normalized_order_type == "MARKET":
            params["priceType"] = "MARKET"
            params["timeInForce"] = "IOC"
        else:
            raise ToobitAPIError(f"Unsupported futures order type: {order_type}")
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)

        payload = await self._private_symbol_request(
            "POST",
            self.futures_order_v1_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        return FuturesOrder(payload)

    async def open_long(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        leverage: int | None = None,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        client_order_id: str | None = None,
        use_demo_symbol: bool = False,
    ) -> FuturesOrder:
        return await self.place_order(
            symbol=symbol,
            side="BUY_OPEN",
            order_type="MARKET",
            quantity=quantity,
            leverage=leverage,
            take_profit=take_profit,
            stop_loss=stop_loss,
            client_order_id=client_order_id,
            use_demo_symbol=use_demo_symbol,
        )

    async def open_short(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        leverage: int | None = None,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        client_order_id: str | None = None,
        use_demo_symbol: bool = False,
    ) -> FuturesOrder:
        return await self.place_order(
            symbol=symbol,
            side="SELL_OPEN",
            order_type="MARKET",
            quantity=quantity,
            leverage=leverage,
            take_profit=take_profit,
            stop_loss=stop_loss,
            client_order_id=client_order_id,
            use_demo_symbol=use_demo_symbol,
        )

    async def flash_close(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str | None = None,
        use_demo_symbol: bool = False,
    ) -> dict[str, Any]:
        """
        Flash close (market close at best price).
        side: LONG or SHORT (the current position side to close)
        """
        params: dict[str, object] = {
            "side": side.strip().upper(),
            "clientOrderId": client_order_id or f"close_{int(time.time() * 1000)}",
        }
        payload = await self._private_symbol_request(
            "POST",
            self.futures_flash_close_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        return payload if isinstance(payload, dict) else {}

    async def set_trading_stop(
        self,
        *,
        symbol: str,
        side: str,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        tp_quantity: Decimal | None = None,
        sl_quantity: Decimal | None = None,
        tp_trigger_by: str = "CONTRACT_PRICE",
        sl_trigger_by: str = "CONTRACT_PRICE",
        stop_type: str = "FIXED_STOP",
        active_price: Decimal | None = None,
        fallback_type: str | None = None,
        fallback_quantity: Decimal | None = None,
        use_demo_symbol: bool = False,
    ) -> dict[str, Any]:
        spec = await self._require_order_spec(symbol)
        params: dict[str, object] = {
            "side": side.strip().upper(),
            "stopType": stop_type.strip().upper(),
        }
        normalized_side = side.strip().upper()
        if take_profit is not None:
            params["takeProfit"] = _fmt_decimal(
                _normalize_take_profit_price(
                    take_profit,
                    spec,
                    position_side=normalized_side,
                )
            )
            params["tpTriggerBy"] = tp_trigger_by.strip().upper()
        if stop_loss is not None:
            params["stopLoss"] = _fmt_decimal(
                _normalize_stop_loss_price(
                    stop_loss,
                    spec,
                    position_side=normalized_side,
                )
            )
            params["slTriggerBy"] = sl_trigger_by.strip().upper()
        if tp_quantity is not None:
            params["tpSize"] = _fmt_decimal(
                _to_exchange_trading_stop_quantity(tp_quantity, spec)
            )
        if sl_quantity is not None:
            params["slSize"] = _fmt_decimal(
                _to_exchange_trading_stop_quantity(sl_quantity, spec)
            )
        if active_price is not None:
            params["activePrice"] = _fmt_decimal(
                _normalize_price_to_tick(active_price, spec, rounding=ROUND_DOWN)
            )
        if fallback_type is not None:
            params["fallbackType"] = fallback_type.strip().upper()
        if fallback_quantity is not None:
            params["fallbackQuantity"] = str(fallback_quantity)

        payload = await self._private_symbol_request(
            "POST",
            self.futures_position_trading_stop_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        return payload if isinstance(payload, dict) else {}

    async def close_long(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str | None = None,
        use_demo_symbol: bool = False,
    ) -> FuturesOrder:
        return await self.place_order(
            symbol=symbol, side="SELL_CLOSE", order_type="MARKET",
            quantity=quantity, client_order_id=client_order_id, use_demo_symbol=use_demo_symbol,
        )

    async def close_short(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str | None = None,
        use_demo_symbol: bool = False,
    ) -> FuturesOrder:
        return await self.place_order(
            symbol=symbol, side="BUY_CLOSE", order_type="MARKET",
            quantity=quantity, client_order_id=client_order_id, use_demo_symbol=use_demo_symbol,
        )

    async def cancel_order(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        orig_client_order_id: str | None = None,
        order_type: str | None = None,
        use_demo_symbol: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, object] = {}
        if order_id:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        if order_type:
            params["type"] = order_type.strip().upper()
        if not params:
            raise ToobitAPIError("order_id or orig_client_order_id is required")
        payload = await self._private_symbol_request(
            "DELETE",
            self.futures_order_v1_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        return payload if isinstance(payload, dict) else {}

    async def get_open_orders(
        self,
        symbol: str | None = None,
        *,
        order_type: str | None = None,
        use_demo_symbol: bool = False,
    ) -> list[FuturesOrder]:
        params: dict[str, object] = {}
        if order_type:
            params["type"] = order_type.strip().upper()
        if symbol:
            payload = await self._private_symbol_request(
                "GET",
                self.futures_open_orders_path,
                symbol=symbol,
                use_demo_symbol=use_demo_symbol,
                params=params,
            )
        else:
            payload = await self._client.signed_request(
                "GET",
                self.futures_open_orders_path,
                params=_demo_private_context_params(use_demo_symbol),
            )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesOrder(item) for item in (items if isinstance(items, list) else [])]

    async def get_open_algo_orders(
        self,
        symbol: str | None = None,
        *,
        stop_category: str = "STOP_PROFIT_LOSS",
        use_demo_symbol: bool = False,
        limit: int = 50,
    ) -> list[FuturesOrder]:
        params: dict[str, object] = {
            "stopCategory": stop_category,
            "limit": limit,
        }
        if symbol:
            params["symbol"] = to_exchange_futures_symbol(
                symbol,
                use_demo_symbol=use_demo_symbol,
            )
        params.update(_demo_private_context_params(use_demo_symbol))
        payload = await self._client.signed_request(
            "GET",
            self.futures_v2_open_algo_orders_path,
            params=params,
        )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesOrder(item) for item in (items if isinstance(items, list) else [])]

    async def cancel_algo_order(self, order_id: str) -> dict[str, Any]:
        payload = await self._client.signed_request(
            "DELETE",
            self.futures_v2_algo_order_path,
            params={"orderId": order_id},
        )
        return payload if isinstance(payload, dict) else {}

    async def get_order_history(
        self,
        symbol: str,
        limit: int = 20,
        *,
        order_id: str | None = None,
        order_type: str | None = None,
        use_demo_symbol: bool = False,
    ) -> list[FuturesOrder]:
        params: dict[str, object] = {
            "limit": limit,
        }
        if order_id:
            params["orderId"] = order_id
        if order_type:
            params["type"] = order_type.strip().upper()
        payload = await self._private_symbol_request(
            "GET",
            self.futures_history_orders_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesOrder(item) for item in (items if isinstance(items, list) else [])]

    async def get_order(
        self,
        *,
        symbol: str,
        order_id: str | None = None,
        orig_client_order_id: str | None = None,
        order_type: str | None = None,
        use_demo_symbol: bool = False,
    ) -> FuturesOrder:
        params: dict[str, object] = {}
        if order_id:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        if order_type:
            params["type"] = order_type.strip().upper()
        if not params:
            raise ToobitAPIError("order_id or orig_client_order_id is required")
        payload = await self._private_symbol_request(
            "GET",
            self.futures_order_v1_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        return FuturesOrder(payload)

    async def get_user_trades(
        self,
        symbol: str,
        limit: int = 50,
        *,
        use_demo_symbol: bool = False,
    ) -> list[FuturesUserTrade]:
        params: dict[str, object] = {
            "limit": limit,
        }
        payload = await self._private_symbol_request(
            "GET",
            self.futures_user_trades_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesUserTrade(item) for item in (items if isinstance(items, list) else [])]

    async def wait_for_order_fill(
        self,
        *,
        symbol: str,
        order_id: str,
        use_demo_symbol: bool = False,
        timeout_seconds: float = 8.0,
        poll_interval_seconds: float = 0.5,
    ) -> tuple[FuturesOrder | None, list[FuturesUserTrade]]:
        deadline = time.monotonic() + timeout_seconds
        last_order: FuturesOrder | None = None
        last_fills: list[FuturesUserTrade] = []
        while time.monotonic() < deadline:
            try:
                history = await self.get_order_history(
                    symbol,
                    limit=50,
                    use_demo_symbol=use_demo_symbol,
                )
                last_order = next(
                    (item for item in history if item.order_id == order_id),
                    last_order,
                )
            except Exception:
                pass
            try:
                fills = await self.get_user_trades(
                    symbol,
                    limit=100,
                    use_demo_symbol=use_demo_symbol,
                )
                last_fills = [item for item in fills if item.order_id == order_id]
            except Exception:
                pass
            if last_order is not None:
                status = last_order.status.upper()
                if status == "FILLED" and last_fills:
                    return last_order, last_fills
                if status in {"PARTIALLY_FILLED", "PARTIAL_FILLED"} and last_order.executed_qty > 0:
                    return last_order, last_fills
                if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                    return last_order, last_fills
            await asyncio.sleep(poll_interval_seconds)
        return last_order, last_fills

    # ── Leverage ──────────────────────────────────────────────────────────────

    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
        *,
        use_demo_symbol: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "leverage": leverage,
        }
        payload = await self._private_symbol_request(
            "POST",
            self.futures_leverage_path,
            symbol=symbol,
            use_demo_symbol=use_demo_symbol,
            params=params,
        )
        return payload if isinstance(payload, dict) else {}

    async def normalize_trade_protection(
        self,
        *,
        symbol: str,
        side: str,
        stop_loss: Decimal | None = None,
        take_profits: list[Decimal] | None = None,
        use_demo_symbol: bool = False,
    ) -> tuple[Decimal | None, list[Decimal]]:
        del use_demo_symbol  # Contract precision is shared with production specs.
        spec = await self._require_order_spec(symbol)
        normalized_side = side.strip().upper()
        normalized_stop_loss = (
            _normalize_stop_loss_price(
                stop_loss,
                spec,
                position_side=normalized_side,
            )
            if stop_loss is not None
            else None
        )
        normalized_take_profits = [
            _normalize_take_profit_price(
                take_profit,
                spec,
                position_side=normalized_side,
            )
            for take_profit in (take_profits or [])
        ]
        return normalized_stop_loss, normalized_take_profits

    async def _require_order_spec(self, symbol: str) -> FuturesContractSpec:
        spec = await self.get_contract_spec(symbol, use_demo_symbol=False)
        if spec is None:
            raise ToobitAPIError(f"Symbol {symbol} is not listed on Toobit futures")
        return spec

    async def _private_symbol_request(
        self,
        method: str,
        path: str,
        *,
        symbol: str,
        use_demo_symbol: bool,
        params: dict[str, object],
    ) -> Any:
        exchange_symbol = to_exchange_futures_symbol(symbol, use_demo_symbol=use_demo_symbol)
        request_params = dict(params)
        request_params["symbol"] = exchange_symbol
        request_params.update(_demo_private_context_params(use_demo_symbol))
        try:
            return await self._client.signed_request(
                method,
                path,
                params=request_params,
            )
        except ToobitAPIError as exc:
            if _should_retry_demo_private_symbol_with_live_symbol(
                use_demo_symbol=use_demo_symbol,
                attempted_symbol=exchange_symbol,
                exc=exc,
            ):
                retry_params = dict(request_params)
                retry_params["symbol"] = to_exchange_futures_symbol(
                    symbol,
                    use_demo_symbol=False,
                )
                try:
                    return await self._client.signed_request(
                        method,
                        path,
                        params=retry_params,
                    )
                except ToobitAPIError as retry_exc:
                    if _should_rewrite_demo_private_symbol_error(
                        use_demo_symbol=use_demo_symbol,
                        attempted_symbol=exchange_symbol,
                        exc=retry_exc,
                        demo_private_symbol_mode=self.demo_private_symbol_mode,
                    ):
                        raise ToobitAPIError(
                            "Toobit demo order rejected for "
                            f"{exchange_symbol}: demo futures must use the TBV symbol family "
                            "through the production private API with business_type=VIRTUAL",
                            status_code=retry_exc.status_code,
                            error_code=retry_exc.error_code,
                            payload=retry_exc.payload,
                        ) from retry_exc
                    raise
            if _should_rewrite_demo_private_symbol_error(
                use_demo_symbol=use_demo_symbol,
                attempted_symbol=exchange_symbol,
                exc=exc,
                demo_private_symbol_mode=self.demo_private_symbol_mode,
            ):
                raise ToobitAPIError(
                    "Toobit demo order rejected for "
                    f"{exchange_symbol}: demo futures must use the TBV symbol family "
                    "through the production private API with business_type=VIRTUAL",
                    status_code=exc.status_code,
                    error_code=exc.error_code,
                    payload=exc.payload,
                ) from exc
            raise

    # ── Market Data ───────────────────────────────────────────────────────────

    async def get_mark_price(self, symbol: str, *, use_demo_symbol: bool = False) -> Decimal:
        from triak_trade.core.symbols import futures_contract_symbol_candidates
        if use_demo_symbol:
            candidates = [to_demo_futures_symbol(symbol)]
        else:
            candidates = futures_contract_symbol_candidates(symbol) or [to_futures_symbol(symbol)]
        for candidate in candidates:
            try:
                payload = await self._client.public_request(
                    "GET", self.mark_price_path, params={"symbol": candidate}
                )
                price = payload.get("price") if isinstance(payload, dict) else None
                if price is not None:
                    return Decimal(str(price))
            except Exception:
                continue
        raise ToobitAPIError(f"Could not fetch mark price for {symbol}")

    async def get_ticker_24hr(
        self,
        symbol: str,
        *,
        use_demo_symbol: bool = False,
    ) -> dict[str, Any]:
        fsym = to_exchange_futures_symbol(symbol, use_demo_symbol=use_demo_symbol)
        return await self._client.public_request(
            "GET", self.ticker_24hr_path, params={"symbol": fsym}
        )

    # ── WebSocket Listen Key ──────────────────────────────────────────────────

    async def create_listen_key(self) -> str:
        payload = await self._client.signed_request("POST", self.futures_listen_key_path)
        key = payload.get("listenKey") if isinstance(payload, dict) else None
        if not key:
            raise ToobitAPIError("No listenKey in response")
        return str(key)

    async def extend_listen_key(self, listen_key: str) -> None:
        params: dict[str, object] = {"listenKey": listen_key}
        await self._client.signed_request("PUT", self.futures_listen_key_path, params=params)

    async def delete_listen_key(self, listen_key: str) -> None:
        params: dict[str, object] = {"listenKey": listen_key}
        await self._client.signed_request("DELETE", self.futures_listen_key_path, params=params)


# ── Factory ────────────────────────────────────────────────────────────────────

def build_futures_client_from_settings(settings: object) -> ToobitFuturesClient:
    from triak_trade.config.settings import Settings
    s: Settings = settings  # type: ignore[assignment]
    base_client = ToobitClient(
        base_url=s.TOOBIT_BASE_URL,
        api_key=s.TOOBIT_API_KEY.get_secret_value(),
        api_secret=s.TOOBIT_API_SECRET.get_secret_value(),
        timeout_seconds=s.TOOBIT_FUTURES_TIMEOUT_SECONDS,
        recv_window=s.TOOBIT_RECV_WINDOW,
        time_path=s.TOOBIT_TIME_PATH,
        exchange_info_path=s.TOOBIT_EXCHANGE_INFO_PATH,
    )
    return ToobitFuturesClient(
        client=base_client,
        demo_private_symbol_mode=s.TOOBIT_DEMO_PRIVATE_SYMBOL_MODE,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _int(value: object) -> int:
    try:
        return int(str(value).split(".")[0])
    except Exception:
        return 0


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return units * step


def _round_to_step(value: Decimal, step: Decimal, *, rounding: str) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).quantize(Decimal("1"), rounding=rounding)
    return units * step


def _to_exchange_contract_quantity(quantity: Decimal, spec: FuturesContractSpec) -> Decimal:
    if quantity <= 0:
        raise ToobitAPIError("quantity must be positive")
    if spec.contract_multiplier <= 0:
        return quantity
    exchange_qty = quantity / spec.contract_multiplier
    step = spec.contract_step_size
    if step > 0:
        exchange_qty = _round_down_to_step(exchange_qty, step)
    min_qty = spec.contract_min_qty
    if min_qty > 0 and exchange_qty < min_qty:
        exchange_qty = min_qty
    max_qty = spec.contract_max_qty
    if max_qty > 0 and exchange_qty > max_qty:
        raise ToobitAPIError(
            f"Order quantity {quantity} exceeds exchange max contracts for {spec.symbol}"
        )
    if exchange_qty <= 0:
        raise ToobitAPIError("Order quantity rounds to zero contracts")
    return exchange_qty


def _from_exchange_contract_quantity(quantity: Decimal, spec: FuturesContractSpec) -> Decimal:
    if quantity <= 0:
        return Decimal("0")
    if spec.contract_multiplier <= 0:
        return quantity
    return quantity * spec.contract_multiplier


def _to_exchange_trading_stop_quantity(quantity: Decimal, spec: FuturesContractSpec) -> Decimal:
    if quantity <= 0:
        raise ToobitAPIError("quantity must be positive")
    if spec.contract_multiplier <= 0:
        return quantity
    exchange_qty = (quantity / spec.contract_multiplier).quantize(
        Decimal("1"),
        rounding=ROUND_DOWN,
    )
    if exchange_qty <= 0:
        exchange_qty = Decimal("1")
    return exchange_qty


def _normalize_price_to_tick(
    value: Decimal,
    spec: FuturesContractSpec,
    *,
    rounding: str,
) -> Decimal:
    if value <= 0:
        raise ToobitAPIError("price must be positive")
    tick_size = spec.tick_size
    if tick_size <= 0:
        return value
    normalized = _round_to_step(value, tick_size, rounding=rounding)
    if normalized <= 0:
        raise ToobitAPIError("price rounds to zero for exchange tick size")
    return normalized


def _normalize_limit_price(
    value: Decimal,
    spec: FuturesContractSpec,
    *,
    side: str,
) -> Decimal:
    normalized_side = side.strip().upper()
    rounding = ROUND_UP if normalized_side.startswith("BUY") else ROUND_DOWN
    return _normalize_price_to_tick(value, spec, rounding=rounding)


def _normalize_stop_loss_price(
    value: Decimal,
    spec: FuturesContractSpec,
    *,
    position_side: str,
) -> Decimal:
    normalized_side = position_side.strip().upper()
    rounding = ROUND_UP if normalized_side == "LONG" else ROUND_DOWN
    return _normalize_price_to_tick(value, spec, rounding=rounding)


def _normalize_take_profit_price(
    value: Decimal,
    spec: FuturesContractSpec,
    *,
    position_side: str,
) -> Decimal:
    normalized_side = position_side.strip().upper()
    rounding = ROUND_DOWN if normalized_side == "LONG" else ROUND_UP
    return _normalize_price_to_tick(value, spec, rounding=rounding)


def _fmt_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _demo_private_context_params(use_demo_symbol: bool) -> dict[str, object]:
    if not use_demo_symbol:
        return {}
    return {"business_type": "VIRTUAL"}

def _should_rewrite_demo_private_symbol_error(
    *,
    use_demo_symbol: bool,
    attempted_symbol: str,
    exc: ToobitAPIError,
    demo_private_symbol_mode: str,
) -> bool:
    if not use_demo_symbol or not attempted_symbol.startswith(DEMO_SYMBOL_PREFIX):
        return False
    return demo_private_symbol_mode == "tbv_only" and exc.error_code in {-1130, -1131}


def _should_retry_demo_private_symbol_with_live_symbol(
    *,
    use_demo_symbol: bool,
    attempted_symbol: str,
    exc: ToobitAPIError,
) -> bool:
    if not use_demo_symbol or not attempted_symbol.startswith(DEMO_SYMBOL_PREFIX):
        return False
    return exc.error_code in {-1130, -1131}
