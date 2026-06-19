# ۰۷ — داده‌ی بازار، تنظیمات و گاردهای امنیتی

## تأمین‌کننده‌های داده‌ی بازار (`market_data/`)

### factory (`factory.py`)
`build_backtest_market_data_provider(settings)`:
- primary = `binance_public` (پیش‌فرض) یا `toobit`.
- اگر `BACKTEST_MARKET_DATA_USE_TOOBIT_FALLBACK=true` → `CompositeMarketDataProvider([primary, toobit])`.

### `BinancePublicFuturesProvider` (`binance_public.py`)
- **آرشیو عمومی Binance** (بدون auth): zipهای daily/monthly کندل futures (`data/futures/um/...`).
  - `_build_archive_specs` ماه کامل را monthly و بقیه را daily می‌گیرد (ماه فعلی همیشه daily).
  - کش روی دیسک (`cache_dir`)؛ ۴۰۴ با فایل `.missing` علامت می‌خورد تا دوباره تلاش نشود.
- اگر آرشیو خالی بود → fallback به **REST klines** (`_load_recent_rest_candles`) با pagination (limit ۱۵۰۰) و کش JSON.
- پارس CSV/REST → `Candle` با `Decimal` و `source=BINANCE`.
- مدیریت خطا: `MarketDataTimeoutError/ConnectionError/HTTPError/ParseError`.

### `ToobitMarketDataProvider` (`toobit.py`)
کندل عمومی Toobit (spot/contract/index/mark)؛ فقط endpointهای عمومی (طبق `AGENTS.md` هرگز signed/private).

### `CompositeMarketDataProvider` (`composite.py`)
به‌ترتیب providerها را امتحان می‌کند؛ اولین نتیجه‌ی غیرخالی را برمی‌گرداند؛ اگر همه خطا دادند آخرین خطا را raise می‌کند.

### `CandleCacheService` (`candle_cache.py`)
کش مبتنی بر DB: کندل‌های موجود را از repository می‌خواند و فقط بازه‌های گمشده‌ی ابتدا/انتها را fetch می‌کند. (توجه: این سرویس در مسیر `RealBacktestRunner` مستقیماً استفاده **نمی‌شود**؛ runner خودش از provider/factory استفاده می‌کند و کش provider روی دیسک است.)

> ⚠️ در `candle_cache._as_utc`، اگر `tzinfo` نباشد همان‌طور بازگردانده می‌شود ولی اگر باشد به UTC و سپس naive تبدیل می‌شود — اختلاط naive/aware که می‌تواند در مقایسه‌ها لغزش ایجاد کند (فایل ۰۸ W5).

## تنظیمات بک‌تست (`config/settings.py`)

| کلید | پیش‌فرض | توضیح |
|------|---------|-------|
| `BACKTEST_MARKET_DATA_PROVIDER` | `binance_public` | provider اصلی |
| `BACKTEST_MARKET_DATA_USE_TOOBIT_FALLBACK` | `true` | fallback به Toobit |
| `BACKTEST_DEFAULT_INITIAL_BALANCE` | `100` | موجودی اولیه |
| `BACKTEST_DEFAULT_RISK_PER_TRADE_PCT` | `3` | ریسک هر ترید |
| `BACKTEST_DEFAULT_INTERVAL` | `1m` | تایم‌فریم |
| `BACKTEST_LIFECYCLE_REFRESH_INTERVAL` | `5m` | بازه‌ی snapshot زنده |
| `BACKTEST_DEFAULT_FILL_POLICY` | `conservative` | سیاست fill |
| `BACKTEST_MAX_MESSAGES` | `5000` | سقف پیام |
| `BACKTEST_MAX_EFFECTIVE_LEVERAGE` | `25` | سقف اهرم margin |
| `BACKTEST_DEFAULT_STOP_PCT` | `5` | استاپ مصنوعی بدون strategy |
| `REAL_BACKTEST_ENABLED` | `false` | کلید اصلی بک‌تست واقعی |
| `REAL_BACKTEST_MAX_MESSAGES` | `1000` | سقف پیام واقعی |
| `REAL_BACKTEST_ACTIVE_SIGNAL_HOURS` | `24` | انقضای سیگنال |
| `REAL_BACKTEST_USE_AI` | `true` | استفاده از AI |
| `REAL_BACKTEST_USE_REGEX_FALLBACK` | `false` | fallback regex |
| `REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH` | `true` | attach مبهم |
| `REAL_BACKTEST_REPORT_DIR` | `runtime/reports/backtests` | مسیر گزارش |
| `RUN_BACKTEST_INTEGRATION_TESTS` | `0` | گارد تست |

نکته: `BACKTEST_MAX_CANDLES`/`REAL_BACKTEST_MAX_CANDLES` تعریف شده‌اند ولی در مسیر شبیه‌سازی **اعمال نمی‌شوند** (فایل ۰۸ W6).

## گاردهای امنیتی (طبق `AGENTS.md`)

- `EXECUTION_MODE='live'` در `settings.py:285` به‌صراحت **مسدود** است (فقط backtest/paper/demo).
- بک‌تست واقعی پشت چند گارد env (REAL_BACKTEST_ENABLED + سه گارد تست).
- داده‌ی بازار فقط endpoint عمومی؛ هرگز private/signed.
- گزارش‌ها non-secret؛ کانال لاگ همیشه انگلیسی و redact‌شده.
- اگر AI در دسترس نبود، پرچم `ai_used=false` و warning صریح؛ هرگز وانمود نمی‌شود.

## مرزهای زمان (timezone)

- همه‌ی محاسبات داخلی UTC.
- نمایش داشبورد/lifecycle به `Asia/Tehran` (`core/time.TEHRAN_TZ`) تبدیل می‌شود.
- `RealBacktestRunRequest._utc` و `_to_utc` ورودی‌های naive را UTC فرض می‌کنند.
