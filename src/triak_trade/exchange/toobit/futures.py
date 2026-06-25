"""Toobit futures trading client — corrected endpoints verified against live API."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.errors import ToobitAPIError

# ── Symbol Helpers ─────────────────────────────────────────────────────────────

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
    if "-SWAP-USDT" in s:
        return s.replace("-SWAP-USDT", "USDT")
    if "-SWAP-USDC" in s:
        return s.replace("-SWAP-USDC", "USDC")
    return s


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
            if b.asset.upper() == "USDT":
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
            if b.coin.upper() == "USDT":
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
        self.side: str = str(data.get("side", ""))
        self.order_type: str = str(data.get("type", ""))
        self.orig_qty: Decimal = _dec(data.get("origQty", "0"))
        self.executed_qty: Decimal = _dec(data.get("executedQty", "0"))
        self.price: Decimal = _dec(data.get("price", "0"))
        self.avg_price: Decimal = _dec(data.get("avgPrice", "0"))
        self.status: str = str(data.get("status", ""))
        self.leverage: int = _int(data.get("leverage", "1"))
        self.time_in_force: str = str(data.get("timeInForce", "GTC"))
        self.margin_locked: Decimal = _dec(data.get("marginLocked", "0"))
        self.raw: dict[str, Any] = data


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
        # Futures endpoints
        futures_balance_path: str = "/api/v1/futures/balance",
        futures_positions_path: str = "/api/v1/futures/positions",
        futures_order_v2_path: str = "/api/v2/futures/order",
        futures_order_v1_path: str = "/api/v1/futures/order",
        futures_open_orders_path: str = "/api/v1/futures/openOrders",
        futures_leverage_path: str = "/api/v1/futures/leverage",
        futures_flash_close_path: str = "/api/v1/futures/flashClose",
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
        self.futures_balance_path = futures_balance_path
        self.futures_positions_path = futures_positions_path
        self.futures_order_v2_path = futures_order_v2_path
        self.futures_order_v1_path = futures_order_v1_path
        self.futures_open_orders_path = futures_open_orders_path
        self.futures_leverage_path = futures_leverage_path
        self.futures_flash_close_path = futures_flash_close_path
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

    async def get_futures_balance(self) -> list[FuturesBalance]:
        payload = await self._client.signed_request("GET", self.futures_balance_path)
        if isinstance(payload, list):
            return [FuturesBalance(item) for item in payload]
        data = payload.get("data", payload)
        if isinstance(data, list):
            return [FuturesBalance(item) for item in data]
        return []

    async def get_today_pnl(self) -> dict[str, Any]:
        payload = await self._client.signed_request("GET", self.futures_today_pnl_path)
        return payload

    async def get_spot_account(self) -> SpotAccountInfo:
        payload = await self._client.signed_request("GET", self.spot_account_path)
        return SpotAccountInfo(payload if isinstance(payload, dict) else {})

    async def check_api_key(self) -> dict[str, Any]:
        return await self._client.signed_request("GET", self.spot_check_api_key_path)

    async def get_full_account_info(self) -> FuturesAccountInfo:
        """Fetch all account info in parallel (balance + PnL + key type)."""
        import asyncio
        balances_task = asyncio.create_task(self.get_futures_balance())
        pnl_task = asyncio.create_task(self.get_today_pnl())
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

    async def get_positions(self, symbol: str | None = None) -> list[FuturesPosition]:
        params: dict[str, object] = {}
        if symbol:
            params["symbol"] = to_futures_symbol(symbol)
        payload = await self._client.signed_request(
            "GET",
            self.futures_positions_path,
            params=params,
        )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesPosition(item) for item in (items if isinstance(items, list) else [])]

    async def get_open_positions(self, symbol: str | None = None) -> list[FuturesPosition]:
        return [p for p in await self.get_positions(symbol) if p.is_open]

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
    ) -> FuturesOrder:
        """
        Place a futures order via v2 API (supports MARKET type).

        side must be: BUY_OPEN, SELL_OPEN, BUY_CLOSE, SELL_CLOSE
        """
        params: dict[str, object] = {
            "symbol": to_futures_symbol(symbol),
            "side": side.strip().upper(),
            "type": order_type.strip().upper(),
            "quantity": str(quantity),
            "newClientOrderId": client_order_id or f"triak_{int(time.time() * 1000)}",
        }
        if leverage is not None:
            params["leverage"] = str(leverage)
        if order_type.upper() == "LIMIT" and price is not None:
            params["price"] = str(price)
            params["timeInForce"] = time_in_force
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)

        payload = await self._client.signed_request(
            "POST",
            self.futures_order_v2_path,
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
        )

    async def flash_close(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Flash close (market close at best price).
        side: LONG or SHORT (the current position side to close)
        """
        params: dict[str, object] = {
            "symbol": to_futures_symbol(symbol),
            "side": side.strip().upper(),
            "clientOrderId": client_order_id or f"close_{int(time.time() * 1000)}",
        }
        return await self._client.signed_request(
            "POST",
            self.futures_flash_close_path,
            params=params,
        )

    async def close_long(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> FuturesOrder:
        return await self.place_order(
            symbol=symbol, side="SELL_CLOSE", order_type="MARKET",
            quantity=quantity, client_order_id=client_order_id,
        )

    async def close_short(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str | None = None,
    ) -> FuturesOrder:
        return await self.place_order(
            symbol=symbol, side="BUY_CLOSE", order_type="MARKET",
            quantity=quantity, client_order_id=client_order_id,
        )

    async def cancel_order(self, *, symbol: str, order_id: str) -> dict[str, Any]:
        params: dict[str, object] = {
            "symbol": to_futures_symbol(symbol),
            "orderId": order_id,
        }
        return await self._client.signed_request(
            "DELETE",
            self.futures_order_v1_path,
            params=params,
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[FuturesOrder]:
        params: dict[str, object] = {}
        if symbol:
            params["symbol"] = to_futures_symbol(symbol)
        payload = await self._client.signed_request(
            "GET",
            self.futures_open_orders_path,
            params=params,
        )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesOrder(item) for item in (items if isinstance(items, list) else [])]

    async def get_order_history(self, symbol: str, limit: int = 20) -> list[FuturesOrder]:
        params: dict[str, object] = {
            "symbol": to_futures_symbol(symbol),
            "limit": limit,
        }
        payload = await self._client.signed_request(
            "GET",
            self.futures_history_orders_path,
            params=params,
        )
        items = payload if isinstance(payload, list) else (
            payload.get("data", []) if isinstance(payload, dict) else []
        )
        return [FuturesOrder(item) for item in (items if isinstance(items, list) else [])]

    # ── Leverage ──────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        params: dict[str, object] = {
            "symbol": to_futures_symbol(symbol),
            "leverage": leverage,
        }
        return await self._client.signed_request("POST", self.futures_leverage_path, params=params)

    # ── Market Data ───────────────────────────────────────────────────────────

    async def get_mark_price(self, symbol: str) -> Decimal:
        from triak_trade.core.symbols import futures_contract_symbol_candidates
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

    async def get_ticker_24hr(self, symbol: str) -> dict[str, Any]:
        fsym = to_futures_symbol(symbol)
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
    return ToobitFuturesClient(client=base_client)


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
