from __future__ import annotations

from triak_trade.config.settings import Settings
from triak_trade.market_data.binance_public import BinancePublicFuturesProvider
from triak_trade.market_data.composite import CompositeMarketDataProvider
from triak_trade.market_data.factory import build_backtest_market_data_provider
from triak_trade.market_data.toobit import ToobitMarketDataProvider


def test_backtest_market_data_defaults_to_live_venue_then_binance() -> None:
    provider = build_backtest_market_data_provider(Settings(_env_file=None))

    assert isinstance(provider, CompositeMarketDataProvider)
    assert [type(item) for item in provider.providers] == [
        ToobitMarketDataProvider,
        BinancePublicFuturesProvider,
    ]


def test_toobit_primary_does_not_duplicate_itself_without_binance_fallback() -> None:
    provider = build_backtest_market_data_provider(
        Settings(
            _env_file=None,
            BACKTEST_MARKET_DATA_PROVIDER="toobit",
            BACKTEST_MARKET_DATA_USE_BINANCE_FALLBACK=False,
        )
    )

    assert isinstance(provider, ToobitMarketDataProvider)


def test_legacy_binance_primary_keeps_toobit_fallback() -> None:
    provider = build_backtest_market_data_provider(
        Settings(
            _env_file=None,
            BACKTEST_MARKET_DATA_PROVIDER="binance_public",
            BACKTEST_MARKET_DATA_USE_TOOBIT_FALLBACK=True,
        )
    )

    assert isinstance(provider, CompositeMarketDataProvider)
    assert [type(item) for item in provider.providers] == [
        BinancePublicFuturesProvider,
        ToobitMarketDataProvider,
    ]
