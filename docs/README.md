# مستندات Triak_Trade

این پوشه مستندات فنی پروژه‌ی **Triak_Trade** است، با تمرکز ویژه روی **موتور بک‌تست (Backtesting)**.
هدف این است که معماری، جزئیات پیاده‌سازی، تصمیم‌های طراحی، و نقاط ضعف/باگ‌های احتمالی به‌صورت دقیق و قابل‌ردیابی مستند شوند.

> نسخه‌ی پایه‌ی کد در زمان نگارش: `triak-trade 0.1.0` — تاریخ بررسی: ۲۰۲۶/۰۶/۱۹.
> تمام ارجاع‌ها به‌صورت `path:line` آمده‌اند تا مستقیماً قابل‌بازبینی باشند.

## فهرست

| فایل | موضوع |
|------|-------|
| [01-overview.md](01-overview.md) | نمای کلی پروژه، ماژول‌ها و قابلیت‌ها |
| [02-backtesting-architecture.md](02-backtesting-architecture.md) | معماری کلی بک‌تست، جریان داده، اجزا |
| [03-simulator-internals.md](03-simulator-internals.md) | جزئیات شبیه‌ساز معاملات، PnL، fill، snapshot |
| [04-real-backtest-pipeline.md](04-real-backtest-pipeline.md) | خط لوله‌ی بک‌تست واقعی (Telegram + AI + Market Data) |
| [05-scoring-and-reports.md](05-scoring-and-reports.md) | امتیازدهی کانال، متریک‌ها و گزارش‌ها |
| [06-strategies.md](06-strategies.md) | استراتژی‌های مدیریت معامله |
| [07-data-and-config.md](07-data-and-config.md) | داده‌ی بازار، تنظیمات و گاردهای امنیتی |
| [08-bugs-and-recommendations.md](08-bugs-and-recommendations.md) | **باگ‌ها، نقاط ضعف و پیشنهادهای بهبود (مهم)** |

## خلاصه‌ی اجرایی (TL;DR)

Triak_Trade یک پلتفرم ماژولار برای **هوش سیگنال تلگرام + بک‌تست + دمو-تریدینگ + مانیتورینگ** است.
workspace جدید دمو-تریدینگ/لایو-مانیتورینگ در داشبورد
برای sessionهای demo از endpointهای عمومی واقعی Toobit و نمادهای demo از جنس `TBV_...` استفاده می‌کند.
برای endpointهای خصوصی هم همان production API واقعی Toobit با `TBV_...` و `business_type=VIRTUAL` صدا زده می‌شود، بنابراین balance/position/history از خود اکانت دمو Toobit می‌آید.
در لایو/دمو، استاپ‌لاس روی خود Toobit به‌صورت `STOP_PROFIT_LOSS` ثبت می‌شود و از endpointهای واقعی `openOrders` و `order` واکشی/کنسل می‌شود.
اگر سیگنال open ورودیِ صریح نداشته باشد، ورودی به‌صورت market-style از قیمت لحظه‌ای resolve می‌شود.
برای targetها هم هر پله به‌صورت close order جداگانه روی Toobit ثبت و بعد از هر fill دوباره re-arm می‌شود.
sessionهای live فقط با `LIVE_TRADING_LIVE_MODE_ENABLED=true` در `.env.local` باز می‌شوند.

موتور بک‌تست از دو مسیر تشکیل شده:

1. **بک‌تست fixture/داخلی** (`BacktestEngine` + `BacktestSimulator`) — قطعی، بدون شبکه، برای تست و توسعه.
2. **بک‌تست واقعی** (`RealBacktestRunner`) — تاریخچه‌ی واقعی تلگرام (Telethon) + طبقه‌بندی AI (Ajil Gateway) با فالبک regex + کندل‌های عمومی Binance/Toobit. خروجی، شبیه‌سازی رویدادمحور + گزارش + امتیاز کانال است.

نکات کلیدی طراحی:
- همه‌ی مقادیر مالی با `Decimal` (نه `float`).
- سرویس‌های بیرونی پشت interface/adapter قرار دارند و در تست‌ها fake می‌شوند.
- بک‌تست «simulation-only» است و هرگز نباید سفارش واقعی ثبت کند.

**رفع‌شده در این بازبینی** (با تست؛ تفصیل در فایل ۰۸):
- ✅ **B2** — یکپارچگی `total_pnl` با لیست تریدها (انتخاب واحد `primary_trades`).
- ✅ **B3 (کارمزد)** — `BACKTEST_FEE_RATE_PCT` + کسر کارمزد از PnL/balance (اسلیپیج/فاندینگ هنوز باز).
- ✅ **B6** — مخرج `win_rate` فقط روی تریدهای filled.
- ✅ **B10** — warning صریح `account_blown_up=true`.

**یافته‌های باز (هنوز پیشنهادی):**
- **B1** — مشکل **کارایی O(N²)** در شبیه‌سازی زنده‌ی هر پیام (`_update_live_simulation_state`).
- **B4** — **تطبیق نماد ناهمگون** (`==` در مقابل `same_market_symbol`) در `_first_candle_open_after`.
- **B7/B9** — نبود سقف اکسپوژر تجمعی؛ drawdown بر اساس ترتیب لیست نه زمان.
