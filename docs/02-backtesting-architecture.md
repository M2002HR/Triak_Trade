# ۰۲ — معماری بک‌تست

## دو مسیر بک‌تست

پروژه دو مسیر بک‌تست دارد که هسته‌ی شبیه‌ساز مشترکی دارند:

```
            ┌──────────────────────────────────────────────────────────┐
            │                  BacktestSimulator                        │
            │   (هسته‌ی رویدادمحور؛ بدون شبکه؛ کاملاً قطعی و Decimal)     │
            └──────────────────────────────────────────────────────────┘
                        ▲                              ▲
                        │                              │
        ┌───────────────┴───────────┐     ┌────────────┴───────────────────┐
        │      BacktestEngine        │     │      RealBacktestRunner          │
        │  (fixture / in-memory)     │     │  (Telegram واقعی + AI + Market)  │
        └────────────────────────────┘     └──────────────────────────────────┘
```

- **مسیر ۱ — `BacktestEngine`** (`backtesting/engine.py`): قطعی، روی fixtureها یا پیام/کندل تزریق‌شده. برای تست و امتیازدهی استفاده می‌شود. خروجی: `BacktestReport`.
- **مسیر ۲ — `RealBacktestRunner`** (`backtesting/real_runner.py`, ۳۰۹۹ خط): خط لوله‌ی کامل واقعی که در نهایت **همان** `BacktestEngine` را روی رویدادها/کندل‌های واقعی صدا می‌زند، به‌علاوه‌ی trace هر پیام و شبیه‌سازی زنده.

## اجزای ماژول `backtesting/`

| فایل | خطوط | نقش |
|------|------|-----|
| `models.py` | 74 | `BacktestRequest`, `BacktestEvent` (مدل‌های ورودی). |
| `engine.py` | 148 | ارکستراسیون: messages→events→simulate(conservative+optimistic)→score→report. |
| `simulator.py` | 1220 | شبیه‌ساز رویدادمحور؛ fill، SL/TP، partial، leverage، snapshot. |
| `timeline.py` | 80 | تبدیل پیام‌ها به `BacktestEvent` با classifier + `ChannelContext`. |
| `directives.py` | 185 | استخراج دستورهای متنی (close %، breakeven، tp list، close all). |
| `correlation.py` | 164 | حل قطعی `related_signal_id` برای follow-upها. |
| `scoring.py` | 232 | متریک‌ها + امتیاز ۰..۱۰۰ کانال با وزن‌دهی. |
| `report.py` | 156 | فرمت‌دهی JSON / Telegram / Markdown + equity curve + symbol summary. |
| `report_store.py` | 112 | ذخیره‌ی گزارش‌های واقعی روی دیسک. |
| `real_runner.py` | 3099 | خط لوله‌ی واقعی، trace per-message، live snapshot. |
| `telegram_source.py` | 53 | wrapper گرفتن تاریخچه‌ی تلگرام. |
| `symbol_mapper.py` | 19 | re-export توابع نرمال‌سازی نماد از `core.symbols`. |
| `fixtures.py` | 69 | پیام/کندل نمونه. |
| `strategies/` | — | استراتژی‌های مدیریت معامله (فایل ۰۶). |

## مدل‌های ورودی

### `BacktestRequest` (`models.py:15`)
ورودی موتور fixture/داخلی:
- `channel, from_date, to_date, initial_balance, interval, fill_policy, risk_per_trade_pct`
- `use_ai_classifier, use_regex_fallback, max_messages, symbols`
- اعتبارسنجی: `to_date > from_date`، `initial_balance > 0`، `risk_per_trade_pct > 0`، interval معتبر.
- `risk_per_trade_pct` در مسیر فعلی یک `allocation factor` است، نه درصد مستقیم سرمایه.
  درصد واقعی سرمایه‌ی درگیر در هر سیگنال از `factor / leverage` محاسبه می‌شود و سپس
  بین `BACKTEST_MIN_ALLOCATION_PCT` و `BACKTEST_MAX_ALLOCATION_PCT` clamp می‌شود.

### `BacktestEvent` (`models.py:44`)
واحد اتمی timeline که شبیه‌ساز مصرف می‌کند:
- `timestamp, action (SignalAction), signal_id, parsed_signal (ParsedSignal), related_signal_id`
- `source_message_id, source_text, close_fraction, close_all, move_stop_to_entry, leverage`
- اعتبارسنجی: `leverage > 0`، `0 < close_fraction ≤ 1`.

### `ParsedSignal` (`domain/models.py:89`)
خروجی classifier — قلب داده:
- `action (open/close/update_sl/update_tp/update_entry/update_leverage/cancel/ignore/unknown)`
- `market, symbol, side (long/short/buy/sell/unknown), entry_type (market/limit/range/unknown)`
- `entry_low, entry_high, stop_loss, take_profits[], leverage, confidence (0..1)`
- `invalid_reason, source_channel_id, source_message_id, parser_version`

### `Candle` (`domain/models.py:225`)
- `symbol, interval, open_time, close_time, open, high, low, close, volume, source (CandleSource)`
- اعتبارسنجی هندسه‌ی قیمت: `close_time > open_time`، `high ≥ max(o,c,l)`، `low ≤ min(o,c,h)`، `volume ≥ 0`. همه `Decimal`.

## جریان مسیر ۱ (`BacktestEngine`)

`engine.py:62 run_from_events`:

1. ساخت events از messages توسط `BacktestTimelineBuilder` (`timeline.py`).
2. اجرای **دوبار** شبیه‌سازی: یک‌بار `CONSERVATIVE` و یک‌بار `OPTIMISTIC` (برای متریک consistency).
3. `final_balance = max(balance طبق fill_policy درخواست, 0)`.
4. `total_pnl = final_balance - initial_balance`.
5. امتیازدهی با `ChannelScorer.score_with_breakdown`.
6. ساخت `BacktestReport` با `trades = conservative_trades` و افزودن `warnings.append("channel_score=…")`.

> ⚠️ نکته‌ی مهم: `report.trades` همیشه **conservative** است ولی `final_balance/total_pnl` می‌تواند **optimistic** باشد → ناهماهنگی (فایل ۰۸، باگ B2).

## ساخت timeline (`timeline.py`)

`BacktestTimelineBuilder.build` پیام‌ها را به ترتیب تاریخ مرتب می‌کند و برای هر پیام:
- اگر متن خالی بود → رویداد `IGNORE`.
- وگرنه `classifier.classify(message, context)` صدا زده می‌شود.
- اگر `is_potential_new_signal` → `signal_id` جدید با `make_signal_id`.
- اگر follow-up → `related_signal_id` و normal‌سازی action با `normalize_related_signal_action`.
- استخراج `close_fraction` و `move_stop_to_entry` از متن.

این مسیر ساده است؛ منطق پیچیده‌ی correlation/promotion فقط در مسیر واقعی (`real_runner`) وجود دارد.

## نقش `ChannelContext` (`agents/context.py`)

وضعیت per-channel در حافظه: پیام‌های اخیر، نگاشت message→signal، `active_signals`, `pending_signal_ids`, کاتالوگ پیام‌ها، و **merge_signal** که follow-upها را در `current_signal` تاشده می‌کند (entry/SL/TP/leverage را غنی‌سازی، action را با اولویت مقدار غیر-unknown).

> نکته: چون merge_signal، `action` را هم از follow-up می‌گیرد، در مسیر واقعی `_sync_signal_tracking` پس از merge، action رویداد base را به‌زور به `OPEN` برمی‌گرداند تا شبیه‌ساز سیگنال را نادیده نگیرد (`real_runner.py:2073`).

ادامه در [۰۳ — جزئیات شبیه‌ساز](03-simulator-internals.md).
