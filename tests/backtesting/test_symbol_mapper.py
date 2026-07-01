from triak_trade.backtesting.symbol_mapper import (
    canonical_market_symbol,
    futures_contract_symbol_candidates,
    futures_index_symbol_candidates,
    market_symbol_candidates,
    normalize_market_symbol,
    same_market_symbol,
)


def test_market_symbol_candidates_prefer_toobit_futures_contract_format() -> None:
    assert market_symbol_candidates("PLAYUSDT") == ["PLAY-SWAP-USDT", "PLAYUSDT"]
    assert normalize_market_symbol("PLAYUSDT") == "PLAY-SWAP-USDT"


def test_market_symbol_candidates_normalize_noisy_channel_symbols() -> None:
    assert futures_contract_symbol_candidates("PLAY/USD") == ["PLAY-SWAP-USDT"]
    assert futures_index_symbol_candidates("#play") == ["PLAYUSDT"]
    assert futures_contract_symbol_candidates("1000SHIB/USDT") == ["1000SHIB-SWAP-USDT"]
    assert normalize_market_symbol("1000SHIBUSDT") == "1000SHIB-SWAP-USDT"


def test_same_market_symbol_matches_contract_and_index_forms() -> None:
    assert canonical_market_symbol("PLAY-SWAP-USDT") == "PLAYUSDT"
    assert same_market_symbol("PLAYUSDT", "PLAY-SWAP-USDT")
    assert canonical_market_symbol("1000SHIB-SWAP-USDT") == "1000SHIBUSDT"
    assert same_market_symbol("1000SHIBUSDT", "1000SHIB-SWAP-USDT")
