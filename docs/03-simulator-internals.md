# ۰۳ — جزئیات شبیه‌ساز معاملات (`simulator.py`)

`BacktestSimulator` قلب قطعی و بدون شبکه‌ی بک‌تست است. رویدادمحور (event-driven) است: لیست `BacktestEvent` و لیست `Candle` می‌گیرد و لیست `SimulatedTrade` + موجودی نهایی + (اختیاری) snapshotها برمی‌گرداند.

## امضای ورودی

`_simulate_internal` (`simulator.py:166`) پارامترهای کلیدی:
- `events`, `candles`, `initial_balance`, `risk_per_trade_pct`, `fill_policy`
- `active_signal_hours` — انقضای سیگنال باز.
- `max_effective_leverage` — سقف اهرم (None = مدل اهرم خاموش، سایزینگ legacy).
- `default_stop_pct` — استاپ مصنوعی وقتی نه SL هست نه strategy.
- `strategy` — `TradeStrategy` برای استاپ/TP مصنوعی و مدیریت partial.
- `snapshot_interval` — برای snapshotهای دوره‌ای (داشبورد زنده).

دو API عمومی: `simulate(...)` (بدون snapshot) و `simulate_with_snapshots(...)`.

## حلقه‌ی اصلی

برای هر event (به‌ترتیب timestamp):

1. **پردازش کندل‌ها تا زمان event** (`_process_candles_until`، stop_at=event.timestamp): همه‌ی کندل‌هایی که `close_time ≤ event.timestamp` هستند روی پوزیشن‌های باز اعمال می‌شوند (SL/TP/expiry).
2. **اعمال خود event** بسته به `parsed.action`:
   - `OPEN` → باز کردن پوزیشن جدید (سایزینگ، استاپ مصنوعی، فیلتر TP).
   - `CLOSE` + `close_all` → بستن همه‌ی پوزیشن‌ها.
   - follow-up روی `related_signal_id`: `CANCEL`, `CLOSE` (با `close_fraction`), `UPDATE_SL` (یا move-to-entry), `UPDATE_TP`.
3. در حالت snapshot، بعد از هر event یک snapshot «message» ثبت می‌شود.

در پایان: کندل‌های باقیمانده پردازش، سپس هر پوزیشن باز با وضعیت `open_until_end`/`partial_tp_open_until_end` در آخرین قیمت بسته می‌شود (`simulator.py:523`).

## ورود (Entry) — `_find_entry_execution` (`simulator.py:542`)

- فقط کندل‌هایی با `same_market_symbol(c.symbol, symbol)` و `open_time ≥ signal_time` در نظر گرفته می‌شوند → **بدون look-ahead** نسبت به زمان سیگنال.
- `EntryType.MARKET` → فیل در `open` اولین کندل بعد از سیگنال.
- `RANGE` (entry_low و entry_high): اگر کندلی `low ≤ entry_high و high ≥ entry_low` را لمس کند → فیل در **midpoint** بازه. (تصمیم: ساده‌سازی؛ کمی خوش‌بینانه — فایل ۰۸ B5.)
- `LIMIT` (فقط entry_low): اگر `low ≤ entry_low ≤ high` → فیل در `entry_low`.
- اگر هیچ کندلی لمس نکرد → `(None, None)` → ترید `not_filled` ثبت می‌شود (`simulator.py:238`).

## سایزینگ پوزیشن (`simulator.py:271`–367)

```
risk_amount   = balance * risk_per_trade_pct / 100
stop_distance = |entry_price - effective_stop|
qty           = risk_amount / stop_distance          # سایزینگ مبتنی بر ریسک
```

- اگر `balance ≤ 0` → ورود جدید رد می‌شود.
- اگر `stop_distance ≤ 0` → رد.
- **استاپ مؤثر** (`effective_stop`):
  1. `parsed.stop_loss` اگر موجود.
  2. وگرنه `strategy.get_synthetic_stop(...)`.
  3. وگرنه درصد ثابت `default_stop_pct` از entry.
- **اهرم**: اگر `max_effective_leverage` تنظیم شده باشد، `effective_leverage = clamp(signal_leverage, 1, max)` و سقف مقدار بر اساس margin اعمال می‌شود:
  `max_qty_by_margin = balance * effective_leverage / entry_price`؛ اگر `qty` بیشتر بود، clamp و note می‌خورد.
  اگر None باشد، اهرم=۱ و سقف margin اعمال نمی‌شود (سایزینگ legacy).
- **فیلتر TP**: TPهایی که در سمت اشتباه entry هستند حذف می‌شوند (long: tp>entry, short: tp<entry). اگر همه حذف شدند و strategy هست → TP مصنوعی R-multiple.

> ⚠️ هیچ سقف اکسپوژر تجمعی بین چند پوزیشن باز هم‌زمان وجود ندارد؛ هر پوزیشن مستقل با balance لحظه‌ای سایز می‌شود (فایل ۰۸ B7).

## اعمال کندل به پوزیشن — `_apply_candle_to_position` (`simulator.py:720`)

محاسبه‌ی برخوردها:
- `_hit_sl`: short → `high ≥ stop`؛ long → `low ≤ stop` (`simulator.py:1201`).
- `_hit_tp`: short → `low ≤ tp`؛ long → `high ≥ tp`.

سپس:
- **SL و TP در یک کندل**:
  - `CONSERVATIVE`: فرض بدترین حالت → SL اول، کل پوزیشن در stop بسته، status `sl_hit_same_candle`.
  - `OPTIMISTIC`: TP(ها) اول اعمال، اگر باقیمانده ماند SL.
- فقط TP: اعمال TP hits؛ اگر باقیمانده صفر شد → status tp.
- فقط SL: بستن کامل در stop.

این تفاوت conservative/optimistic فقط در «کندل‌های هم‌زمان SL+TP» اثر دارد؛ در بقیه‌ی موارد یکسان است. (پیامد در فایل ۰۸ B2.)

## برداشت سود پلکانی — `_apply_take_profit_hits` (`simulator.py:782`)

برای هر TP لمس‌شده:
- اگر strategy هست: `get_target_hit_action(...)` تعیین می‌کند چه کسری بسته شود و آیا استاپ به entry/سطح جدید منتقل شود (risk-free / trailing).
- اگر strategy نیست: تقسیم مساوی بین TPهای باقیمانده (`1/remaining`).
- `_close_fraction_of_position` کسر مشخص را در قیمت TP می‌بندد، `realized_pnl` را جمع می‌کند، `remaining_quantity` را کم می‌کند، و `targets_hit++`.

## محاسبه‌ی PnL — `_calculate_pnl` (`simulator.py:1213`)

```
direction = -1 if short else +1
pnl = (exit_price - entry_price) * quantity * direction
```

- PnL **دلاری** مستقل از اهرم است (درست).
- در `_finalize_position` (`simulator.py:893`):
  `margin = (entry*original_qty) / leverage`
  `pnl_pct = realized_pnl / margin * 100` → بازده‌ی روی margin (اهرم درصد را تقویت می‌کند).

> ⚠️ `realized_fees` هرگز افزوده نمی‌شود → **کارمزد/اسلیپیج/فاندینگ صفر** است (فایل ۰۸ B3).

## وضعیت‌های ترید (status)

نمونه‌ها: `tp_hit`, `tp_hit_same_candle`, `sl_hit`, `sl_hit_same_candle`, `partial_tp_then_sl`, `partial_tp_complete`, `partial_close_then_tp/sl`, `expired`, `partial_tp_expired`, `cancelled`, `partial_tp_then_cancel`, `manual_close`, `manual_partial_close`, `open_until_end`, `partial_tp_open_until_end`, `not_filled`.

## انقضا — `_expire_position_if_needed` (`simulator.py:699`)

اگر `active_signal_hours` تنظیم شده و `candle.open_time ≥ entry_time + hours` → بستن کامل در `candle.open` با status `expired`.

## بستن دستی / کنسل

- `CANCEL`: کل باقیمانده در `position.entry_price` بسته می‌شود (≈ خروج breakeven) (`simulator.py:409`).
- `CLOSE` (follow-up): قیمت = اولین `open` کندل بعد از event، با fallback به `entry_price`. کسر = `close_fraction or 1`.
- `close_all`: روی همه‌ی پوزیشن‌ها.

> ⚠️ `_first_candle_open_after` (`simulator.py:574`) از تطبیق **دقیق** `c.symbol == symbol` استفاده می‌کند، نه `same_market_symbol` — ناهمگون با بقیه‌ی کد (فایل ۰۸ B4).

## Snapshotها — `_build_snapshot` (`simulator.py:924`)

برای داشبورد زنده، در دو نوع `checkpoint_kind`:
- `message` — بعد از هر event.
- `interval` — در گام‌های `snapshot_interval` (لنگر اولیه با `_initial_snapshot_anchor`).

هر snapshot شامل: موجودی realized/current، PnL realized/unrealized، wins/losses، و `signal_states` (per-signal با تاریخچه‌ی قیمت، تاریخچه‌ی SL/TP به‌صورت `PriceLevelSpan`، margin، leverage و …). این داده‌ها مستقیماً به نمودار سیگنال در داشبورد feed می‌شوند.

## SimulatedTrade (خروجی نهایی، `domain/models.py:255`)

`trade_id, signal_id, channel_id, symbol, side, entry_time, exit_time, entry_price, exit_price, quantity, pnl, pnl_pct, fees, status, notes[]`. `quantity` و `fees` باید `≥ 0` باشند.
