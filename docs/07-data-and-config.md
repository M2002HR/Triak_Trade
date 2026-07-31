# 07 - Data And Configuration

> Last reviewed against the running stack: 2026-07-31.

## Market Data Providers

Triak_Trade keeps market data behind provider interfaces.

Current backtesting options:
- `BinancePublicFuturesProvider`
- `ToobitMarketDataProvider`
- `CompositeMarketDataProvider`

The default factory chain is Toobit primary with Binance public fallback. A legacy
Binance-primary chain can still use Toobit as fallback.

## Binance Public Provider

The Binance public provider supports:
- historical archive downloads
- REST fallback for recent candles
- local disk caching

This is the default fallback historical source for guarded backtests.

## Toobit Public Provider

The Toobit provider uses public endpoints only. It is used for:
- public klines dry-runs
- primary backtest market data
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
| `BACKTEST_FEE_RATE_PCT` | `0.04` |
| `BACKTEST_MARKET_DATA_PROVIDER` | `toobit` |
| `BACKTEST_MARKET_DATA_USE_BINANCE_FALLBACK` | `true` |
| `REAL_BACKTEST_ENABLED` | `false` |
| `REAL_BACKTEST_DEFAULT_CHANNEL` | `https://t.me/Tofan_Trade` |
| `REAL_BACKTEST_DEFAULT_INTERVAL` | `1m` |
| `REAL_BACKTEST_MAX_MESSAGES` | `1000` |
| `REAL_BACKTEST_MAX_CANDLES` | `100000` |
| `REAL_BACKTEST_MAX_CANDLES_PER_SYMBOL` | `90000` |
| `BACKTEST_MAX_CANDLES_PER_SYMBOL` | `10000` |
| `BACKTEST_MARKET_DATA_TIMEOUT_SECONDS` | `180` |
| `BACKTEST_MARKET_DATA_MAX_CONCURRENCY` | `1` |
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
| `LIVE_TRADING_MAX_STOP_LOSS_PCT_OF_BALANCE` | `5` |
| `SIGNAL_CONSOLIDATION_SECONDS` | `180` |
| `LIVE_TRADING_ACCOUNT_POSITION_POLICY` | `net` |
| `LIVE_TRADING_SAME_DIRECTION_POLICY` | `aggregate` |
| `LIVE_TRADING_SYMBOL_RISK_CAP_PCT_OF_BALANCE` | `5` |
| `LIVE_TRADING_DUPLICATE_SIGNAL_WINDOW_SECONDS` | `300` |
| `LIVE_TRADING_DUPLICATE_PRICE_TOLERANCE_BPS` | `20` |
| `LIVE_TRADING_USE_OWNED_V2_STOP_ORDERS` | `true` |
| `LIVE_TRADING_MIN_ALLOCATION_PCT` | `2` |
| `LIVE_TRADING_MAX_ALLOCATION_PCT` | `20` |
| `LIVE_TRADING_PENDING_ENTRY_POLL_SECONDS` | `2` |
| `LIVE_TRADING_PENDING_ENTRY_TTL_SECONDS` | `86400` |
| `LIVE_TRADING_PROTECTION_SYNC_RETRY_ATTEMPTS` | `3` |
| `LIVE_TRADING_PROTECTION_SYNC_RETRY_DELAY_SECONDS` | `1.0` |
| `LIVE_TRADING_EXCHANGE_POSITION_MISS_CONFIRMATIONS` | `2` |
| `LIVE_TRADING_EXCHANGE_POSITION_MISS_GRACE_SECONDS` | `15` |
| `LIVE_TRADING_STOP_COOLDOWN_BASE_SECONDS` | `3600` |
| `LIVE_TRADING_STOP_COOLDOWN_MAX_SECONDS` | `21600` |
| `LIVE_TRADING_MAX_TRIGGER_SLIPPAGE_PCT` | `0.5` |
| `LIVE_TRADING_REQUIRE_AI_CLASSIFIER` | `true` |
| `TOOBIT_DEMO_PRIVATE_SYMBOL_MODE` | `tbv_only` |

Selected dashboard logging defaults:

| Key | Default |
|-----|---------|
| `DASHBOARD_FILE_LOG_ENABLED` | `true` |
| `DASHBOARD_LOG_LEVEL` | `DEBUG` |
| `DASHBOARD_LOG_MAX_BYTES` | `20971520` |
| `DASHBOARD_LOG_BACKUP_COUNT` | `5` |

## Runtime Rules

- The root `.env.local` is the single runtime config source.
- Compose mounts and reads that file directly.
- The Ajil submodule must not have its own runtime `.env`.
- Real log-channel sending is off by default.
- Live session unlock is separate from merely choosing `EXECUTION_MODE=live`.
- Dashboard and CLI backtests inherit live strategy/risk/leverage/fee/consolidation
  defaults unless the run explicitly overrides them.
- The Backtest and Live Trade tabs share
  `runtime/dashboard/state/saved_channels.json`. On first use, existing entries from
  the former live runtime channel file are merged into this shared library once.
- Dashboard file logging rotates at the configured byte limit and keeps the configured
  number of backups; the Logs tab reports both policy and current disk usage.

## Backtest Candle Budget

`REAL_BACKTEST_MAX_CANDLES_PER_SYMBOL=90000` currently means:
- at `1m`, a single symbol can hold about 62.5 days of cached candles
- windows are extended around the signals that actually need them
- later signals in longer backtests are no longer silently starved because an early symbol window consumed the full cap from the global start date

## Current Config Drift To Watch

Historically, `.env.example` has drifted away from `settings.py`.
The most notable examples were:
- stale backtest default risk factor values
- removed admin-bot related keys
- missing newer live-trading/backtesting keys

This review updates both docs and `.env.example` to reflect current repository behavior more closely.
