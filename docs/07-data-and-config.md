# 07 - Data And Configuration

## Market Data Providers

Triak_Trade keeps market data behind provider interfaces.

Current backtesting options:
- `BinancePublicFuturesProvider`
- `ToobitMarketDataProvider`
- `CompositeMarketDataProvider`

The factory can choose a primary provider and optionally use Toobit as fallback.

## Binance Public Provider

The Binance public provider supports:
- historical archive downloads
- REST fallback for recent candles
- local disk caching

This is the main historical source for guarded real backtests.

## Toobit Public Provider

The Toobit provider uses public endpoints only. It is used for:
- public klines dry-runs
- optional fallback market data
- live/demo support where public market prices are needed

## Candle Cache Service

`CandleCacheService` stores and reuses candles through the DB repository layer.

Current caveat:
- `_as_utc()` still mixes naive and aware datetime handling, which is a real correctness risk for boundary comparisons.

## Important Settings

Selected backtesting defaults from `settings.py`:

| Key | Default |
|-----|---------|
| `BACKTEST_DEFAULT_INITIAL_BALANCE` | `100` |
| `BACKTEST_DEFAULT_RISK_PER_TRADE_PCT` | `120` |
| `BACKTEST_MIN_ALLOCATION_PCT` | `2` |
| `BACKTEST_MAX_ALLOCATION_PCT` | `20` |
| `BACKTEST_DEFAULT_INTERVAL` | `1m` |
| `BACKTEST_DEFAULT_FILL_POLICY` | `conservative` |
| `BACKTEST_MAX_MESSAGES` | `5000` |
| `BACKTEST_DEFAULT_STOP_PCT` | `5` |
| `BACKTEST_SYNTHETIC_STOP_MAX_LOSS_PCT_OF_BALANCE` | `5` |
| `BACKTEST_FEE_RATE_PCT` | `0` |
| `REAL_BACKTEST_ENABLED` | `false` |
| `REAL_BACKTEST_DEFAULT_CHANNEL` | `https://t.me/Tofan_Trade` |
| `REAL_BACKTEST_DEFAULT_INTERVAL` | `1m` |
| `REAL_BACKTEST_MAX_MESSAGES` | `1000` |
| `REAL_BACKTEST_MAX_CANDLES` | `100000` |
| `REAL_BACKTEST_MAX_CANDLES_PER_SYMBOL` | `90000` |
| `REAL_BACKTEST_USE_AI` | `true` |
| `REAL_BACKTEST_USE_REGEX_FALLBACK` | `false` |
| `REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N` | `10` |

Selected live/demo defaults:

| Key | Default |
|-----|---------|
| `LIVE_TRADING_ENABLED` | `false` |
| `LIVE_TRADING_MODE` | `demo` |
| `LIVE_TRADING_LIVE_MODE_ENABLED` | `false` |
| `LIVE_TRADING_DEFAULT_RISK_PER_TRADE_PCT` | `120` |
| `LIVE_TRADING_FEE_RATE_PCT` | `0.04` |
| `LIVE_TRADING_MAX_EFFECTIVE_LEVERAGE` | `50` |
| `LIVE_TRADING_MIN_ALLOCATION_PCT` | `2` |
| `LIVE_TRADING_MAX_ALLOCATION_PCT` | `20` |
| `LIVE_TRADING_REQUIRE_AI_CLASSIFIER` | `true` |
| `TOOBIT_DEMO_PRIVATE_SYMBOL_MODE` | `tbv_only` |

## Runtime Rules

- The root `.env.local` is the single runtime config source.
- Compose mounts and reads that file directly.
- The Ajil submodule must not have its own runtime `.env`.
- Real log-channel sending is off by default.
- Live session unlock is separate from merely choosing `EXECUTION_MODE=live`.

## Current Config Drift To Watch

Historically, `.env.example` has drifted away from `settings.py`.
The most notable examples were:
- stale backtest default risk factor values
- removed admin-bot related keys
- missing newer live-trading/backtesting keys

This review updates both docs and `.env.example` to reflect current repository behavior more closely.
