from __future__ import annotations

from triak_trade.backtesting.symbol_mapper import market_symbol_candidates, normalize_market_symbol


def test_symbol_mapper_normalizes_usd_pair_to_usdt() -> None:
    assert normalize_market_symbol("ZAMA/USD") == "ZAMAUSDT"
    assert market_symbol_candidates("ZAMA/USD") == ["ZAMAUSDT", "ZAMAUSD"]


def test_symbol_mapper_cleans_duplicate_usd_usdt_noise() -> None:
    assert normalize_market_symbol("ZAMAUSDUSDT") == "ZAMAUSDT"
    assert market_symbol_candidates("ZAMAUSDUSDT")[0] == "ZAMAUSDT"
