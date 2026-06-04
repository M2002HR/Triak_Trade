"""Market-data provider factories."""

from __future__ import annotations

from triak_trade.config.settings import Settings
from triak_trade.market_data.binance_public import BinancePublicFuturesProvider
from triak_trade.market_data.composite import CompositeMarketDataProvider
from triak_trade.market_data.interfaces import MarketDataProvider
from triak_trade.market_data.toobit import ToobitMarketDataProvider


def build_backtest_market_data_provider(settings: Settings) -> MarketDataProvider:
    primary = _build_primary_provider(settings)
    if not settings.BACKTEST_MARKET_DATA_USE_TOOBIT_FALLBACK:
        return primary
    fallback = _build_toobit_provider(settings)
    return CompositeMarketDataProvider([primary, fallback])


def _build_primary_provider(settings: Settings) -> MarketDataProvider:
    if settings.BACKTEST_MARKET_DATA_PROVIDER == "toobit":
        return _build_toobit_provider(settings)
    return BinancePublicFuturesProvider(
        base_url=settings.BINANCE_PUBLIC_DATA_BASE_URL,
        rest_base_url=settings.BINANCE_FUTURES_REST_BASE_URL,
        klines_path=settings.BINANCE_FUTURES_KLINES_PATH,
        ticker_price_path=settings.BINANCE_FUTURES_TICKER_PRICE_PATH,
        cache_dir=settings.BINANCE_PUBLIC_DATA_CACHE_DIR,
        timeout_seconds=settings.BINANCE_PUBLIC_DATA_TIMEOUT_SECONDS,
    )


def _build_toobit_provider(settings: Settings) -> ToobitMarketDataProvider:
    return ToobitMarketDataProvider(
        base_url=settings.TOOBIT_BASE_URL,
        klines_path=settings.TOOBIT_KLINES_PATH,
        mark_price_klines_path=settings.TOOBIT_FUTURES_MARK_PRICE_KLINES_PATH,
        index_klines_path=settings.TOOBIT_FUTURES_INDEX_KLINES_PATH,
        contract_ticker_price_path=settings.TOOBIT_FUTURES_TICKER_PRICE_PATH,
        timeout_seconds=settings.TOOBIT_MARKET_DATA_TIMEOUT_SECONDS,
        limit=settings.TOOBIT_MARKET_DATA_LIMIT,
    )
