# ۰۸ — باگ‌ها، نقاط ضعف و پیشنهادهای بهبود

> این فایل حساس‌ترین بخش بازبینی است. هر مورد با **شناسه، شدت، محل دقیق، توضیح، و پیشنهاد رفع** آمده.
> شدت‌ها: 🔴 بحرانی · 🟠 مهم · 🟡 متوسط · ⚪ جزئی/بهبود.

## وضعیت رفع‌ها (تا تاریخ بازبینی)

این موارد در همین بازبینی **رفع و با تست پوشش داده شدند** (تست‌ها سبز، `ruff`/`mypy` پاس):

| شناسه | موضوع | وضعیت |
|-------|-------|-------|
| **B2** | یکپارچگی `total_pnl` با لیست تریدها (`primary_trades`) | ✅ رفع شد |
| **B3** | کارمزد معاملاتی (`BACKTEST_FEE_RATE_PCT`, netting در PnL/balance) | ✅ رفع شد (اسلیپیج/فاندینگ هنوز باز) |
| **B4** | تطبیق نماد ناهمگون در `_first_candle_open_after` | ✅ رفع شد |
| **B6** | مخرج `win_rate` فقط روی تریدهای filled | ✅ رفع شد |
| **B10/W10** | warning صریح `account_blown_up=true` | ✅ نیمه‌رفع |
| **W4** | interval در خلاصه تلگرام از `report.interval` (نه `notes[0]`) | ✅ رفع شد |
| **B1** | throttle O(N²): live sim فقط روی signal events + هر N پیام passive | ✅ رفع شد |
| **B7** | سقف margin تجمعی: clamping qty به free_margin قبل از باز کردن | ✅ رفع شد |
| **B9** | sort تریدها بر اساس `exit_time` برای drawdown و equity curve | ✅ رفع شد |
| **W3** | logging.warning قبل از fallback به default strategy | ✅ رفع شد |
| **W6** | اعمال `REAL_BACKTEST_MAX_CANDLES` قبل از simulate | ✅ رفع شد |
| **W8** | profit_factor=None → ∞ در telegram summary | ✅ رفع شد |

فایل‌های تغییر‌یافته: `engine.py`, `scoring.py`, `simulator.py`, `config/settings.py`, `real_runner.py`, `report.py`, `domain/models.py`, `strategies/registry.py` + تست جدید `tests/backtesting/test_backtest_fee_and_consistency.py`.

بقیه‌ی موارد (B5, B8, B11, W1, W2, W5, W7, W9) **هنوز باز** و صرفاً پیشنهادی‌اند.

---

## باگ‌های صحت محاسبات مالی (مهم‌ترین دسته)

### 🔴 B1 — شبیه‌سازی زنده O(N²): بازاجرای کامل به‌ازای هر پیام — ✅ رفع شد (throttle)
**محل**: `real_runner.py` — `_build_events_with_traces` + `_update_live_simulation_state` + `_emit_interval_snapshots`.

**مشکل اصلی:** `_update_live_simulation_state` بعد از **هر** پیام (شامل پیام‌های empty/IGNORE) از صفر شبیه‌سازی می‌کرد → O(N²). `_emit_interval_snapshots` هم هر بار **همه‌ی** snapshotهای تاریخی را emit می‌کرد.

**رفع انجام‌شده:**
1. **Throttle signal-aware**: در حلقه‌ی پیام‌ها (`_build_events_with_traces`)، live sim فقط زمانی اجرا می‌شود که:
   - پیام دارای event سیگنال‌دار (OPEN/CLOSE/CANCEL/UPDATE_*) باشد → همیشه فوری.
   - یا `_passive_since_update >= REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N` passive پیام گذشته باشد (پیش‌فرض ۱۰).
   - بین دو اجرا، آخرین نتیجه‌ی live از cache (`_live_metrics_cache`, `_live_signals_cache`) استفاده می‌شود.
2. **Interval snapshot cursor**: `emitted_interval_count: list[int]` به هر فراخوانی پاس داده می‌شود. `_emit_interval_snapshots` فقط snapshotهای **جدید** (ایندکس > cursor) emit می‌کند — انفجار event برطرف شد.
3. **تنظیم جدید** `REAL_BACKTEST_LIVE_SIM_UPDATE_EVERY_N: int = 10`.

**اثر**: برای ۱۰۰۰ پیام با ۵۰ سیگنال و ۹۵۰ passive، تعداد اجراهای کامل شبیه‌سازی از ۱۰۰۰ به ~۱۴۵ (۵۰ سیگنال + ۹۵ بلوک passive) کاهش می‌یابد.

---

### 🔴 B2 — ناهماهنگی conservative/optimistic بین `total_pnl` و لیست تریدها — ✅ رفع شد
**محل**: `engine.py:96-135`.

**مشکل اصلی (نسخه‌ی قبلی):** `report.trades` همیشه `conservative_trades` بود، ولی `final_balance`/`total_pnl` بسته به `fill_policy` می‌توانست از optimistic بیاید. در نتیجه `win_rate`, `profit_factor`, `max_drawdown` و equity curve از conservative محاسبه می‌شدند ولی `total_pnl` از optimistic → `sum(trade.pnl) ≠ total_pnl` و equity curve به final_balance نمی‌رسید.

**رفع انجام‌شده:** اکنون یک مجموعه‌ی واحد `primary_trades`/`primary_final` مطابق `request.fill_policy` انتخاب و **هم** برای متریک‌ها/scoring و **هم** برای `report.trades` و `total_pnl` استفاده می‌شود:
```python
if request.fill_policy is BacktestFillPolicy.CONSERVATIVE:
    primary_trades, primary_final = conservative_trades, conservative_final
else:
    primary_trades, primary_final = optimistic_trades, optimistic_final
...
trades=primary_trades,                      # report
scorer.score_with_breakdown(trades=primary_trades, ...)
```
اینورینت `sum(trade.pnl) == final_balance - initial_balance` اکنون برقرار است (تست: `tests/backtesting/test_backtest_fee_and_consistency.py`).

> نکته‌ی باقی‌مانده: در حالت blow-up (balance منفی)، clamp شدن `final_balance` به صفر این اینورینت را می‌شکند؛ این حالت اکنون با warning صریح `account_blown_up=true` علامت‌گذاری می‌شود (B10).

---

### 🔴 B3 — کارمزد مدل نشده — ✅ رفع شد (کارمزد) · ⚠️ اسلیپیج/فاندینگ هنوز باز
**محل**: `simulator.py` (`_simulate_internal` entry fee، `_close_fraction_of_position:879` exit fee، `_finalize_position:921` netting)، `settings.py:BACKTEST_FEE_RATE_PCT`.

**مشکل اصلی (نسخه‌ی قبلی):** `realized_fees` هرگز افزایش نمی‌یافت و `pnl = realized_pnl` بدون کسر کارمزد بود → PnL سیستماتیک خوش‌بینانه.

**رفع انجام‌شده — کارمزد:**
- تنظیم جدید `BACKTEST_FEE_RATE_PCT` (پیش‌فرض `0` → بدون تغییر رفتار) که درصد کارمزد per-side روی notional است.
- در باز کردن پوزیشن: `entry_fee = entry_price * qty * fee_rate/100` به `realized_fees` افزوده می‌شود.
- در هر بستن (کامل/partial): `exit_fee = exit_price * quantity * fee_rate/100` به `realized_fees` افزوده می‌شود.
- در `_finalize_position`: `pnl = realized_pnl - realized_fees` (net) و `fees = realized_fees` جداگانه گزارش می‌شود. چون balance از `trade.pnl` ساخته می‌شود، کارمزد به‌طور سازگار در balance/total_pnl/scoring/drawdown و snapshotهای زنده اعمال می‌شود (اینورینت B2 حفظ می‌شود).
- plumbing: `engine.run_from_events(fee_rate_pct=...)` و در `real_runner` از `settings.BACKTEST_FEE_RATE_PCT` به هر دو `simulate` و `simulate_with_snapshots` پاس داده می‌شود.
- تست: `tests/backtesting/test_backtest_fee_and_consistency.py`.

**باقی‌مانده (باز):**
- **اسلیپیج** هنوز مدل نشده (fill دقیقاً روی قیمت تئوریک). پیشنهاد: `slippage_bps` روی قیمت fill در جهت نامطلوب.
- **فاندینگ** برای فیوچرز ۲۴ساعته هنوز نیست. پیشنهاد: `funding_rate × مدت نگهداری` روی پوزیشن‌های باز.

---

### 🟠 B4 — تطبیق نماد ناهمگون (`==` به‌جای `same_market_symbol`) — ✅ رفع شد
**محل**: `simulator.py:596 _first_candle_open_after`.

**مشکل اصلی (نسخه‌ی قبلی):** `c.symbol == symbol` (مقایسه‌ی دقیق رشته) در تنها تابعی که قیمت خروج دستی (`CLOSE`/`close_all`) را می‌یابد. اگر `position.symbol` فرمت متفاوتی داشت (مثلاً `BTC-SWAP-USDT` از Toobit در مقابل کندل `BTCUSDT` از Binance Public)، lookup شکست می‌خورد و fallback به `entry_price` → PnL اشتباه صفر.

**رفع انجام‌شده:** خط ۵۹۶ اکنون:
```python
candle = next((c for c in candles if same_market_symbol(c.symbol, symbol) and ...), None)
```
مطابق `_last_price` و `_find_entry_execution` که هر دو از `same_market_symbol` استفاده می‌کردند. `same_market_symbol` از `canonical_market_symbol` (در `core/symbols.py`) استفاده می‌کند که SWAP-variants را نرمال می‌کند — و قبلاً import بود.

---

### 🟠 B5 — fill در midpoint برای entry بازه‌ای (look-ahead خفیف)
**محل**: `simulator.py:557-564`.

برای `RANGE`، به‌محض اینکه کندلی بازه را لمس کند، فیل در **midpoint** بازه انجام می‌شود — صرف‌نظر از اینکه قیمت از کدام سمت وارد بازه شده. اگر قیمت فقط لبه‌ی بازه را لمس کرده باشد، midpoint قیمتی است که شاید هرگز معامله نشده.

**اثر**: entry خوش‌بینانه/بدبینانه بسته به جهت؛ سوگیری کوچک ولی سیستماتیک.

**پیشنهاد**: فیل در نزدیک‌ترین لبه‌ی بازه به قیمت ورودِ کندل (یا لبه‌ای که اول لمس می‌شود)، یا حداقل مستندسازی صریح این فرض به‌عنوان حالت optimistic.

---

### 🟠 B6 — مخرج `win_rate` شامل تریدهای پر-نشده و breakeven — ✅ رفع شد
**محل**: `scoring.py:99-105`.

**مشکل اصلی (نسخه‌ی قبلی):** `win_rate = wins / len(trades)` که `not_filled` و breakeven را هم در مخرج می‌آورد، در حالی‌که `profit_factor`/`fill_rate` روی `filled_trades` کار می‌کردند → `win_rate` سیستماتیک کم‌برآورد و در نتیجه `win_rate_score` (وزن ۰.۱۸) ناعادلانه پایین.

**رفع انجام‌شده:** مخرج اکنون `filled_trades` است:
```python
filled_trades = [t for t in trades if t.status != "not_filled"]
wins = sum(1 for t in filled_trades if t.pnl > 0)
win_rate = Decimal(wins) / Decimal(len(filled_trades)) if filled_trades else Decimal("0")
```
این `win_rate` را با `profit_factor` و `fill_rate` هم‌مقیاس می‌کند. تست: `test_backtest_fee_and_consistency.py::test_win_rate_denominator_excludes_not_filled_and_breakeven`.

> نکته: تریدهای breakeven (`pnl==0`) هنوز در مخرج هستند ولی win شمرده نمی‌شوند. اگر بخواهید breakeven کاملاً خنثی باشد، می‌توان آن‌ها را از مخرج هم حذف کرد — این یک تصمیم محصولی است (هنوز باز).

---

### 🟠 B7 — نبود سقف اکسپوژر/margin تجمعی بین پوزیشن‌های هم‌زمان — ✅ رفع شد
**محل**: `simulator.py:330-351` (بعد از سایزینگ اهرمی، قبل از فیلتر TP).

**مشکل اصلی:** هر OPEN مستقل با `balance` سایز می‌شد. مجموع margin چند پوزیشن هم‌زمان می‌توانست از balance بیشتر شود.

**رفع انجام‌شده:**
```python
used_margin = sum((pos.entry_price * pos.original_quantity) / pos.effective_leverage
                  for pos in open_positions.values())
new_margin = (entry_price * qty) / effective_leverage
if used_margin + new_margin > balance:
    free_margin = max(balance - used_margin, Decimal("0"))
    if free_margin <= Decimal("0"):
        notes.append("rejected_insufficient_portfolio_margin"); continue
    qty = (free_margin * effective_leverage) / entry_price  # clamp
    notes.append(f"quantity_capped_portfolio_margin; ...")
```
اگر free margin کافی نباشد: qty clamp به مقدار واقعی، یا رد کامل اگر free_margin صفر باشد. هر دو trace در `notes` ثبت می‌شوند.

---

### 🟡 B8 — بازنویسی کامل فایل JSON به‌ازای هر event داشبورد
**محل**: `dashboard/backtest_runtime.py:371 _handle_progress` → `store.write(run)`.

هر progress event کل state run (شامل لیست `messages` با `full_text` همه‌ی پیام‌ها و `events`) را serialize و روی دیسک بازنویسی می‌کند. همراه با B1 (انفجار رویداد)، I/O به‌شدت بالا می‌رود. `events` به ۲۵۰ محدود شده ولی `messages` و `signals` رشد می‌کنند.

**پیشنهاد**: نوشتن را throttle/debounce کنید (مثلاً هر ۲۵۰ms یا در پایان batch)؛ یا فقط delta را در یک append-log بنویسید و snapshot کامل را دوره‌ای ذخیره کنید.

---

### 🟡 B9 — drawdown و equity curve بر اساس ترتیب لیست تریدها، نه زمان — ✅ رفع شد
**محل**: `scoring.py:218 _max_drawdown`، `report.py:139 _equity_curve`.

**مشکل اصلی:** ترتیب تریدها در لیست لزوماً ترتیب زمانی نبود.

**رفع انجام‌شده:** هر دو تابع اکنون قبل از محاسبه روی `exit_time` مرتب می‌کنند:
```python
sorted_trades = sorted(trades, key=lambda t: t.exit_time or datetime(9999, 12, 31, tzinfo=timezone.utc))
```
تریدهای بدون `exit_time` (not_filled) به انتها می‌روند و چون PnL صفر دارند، ترتیبشان بی‌اثر است. `import timezone` به هر دو فایل اضافه شد تا sentinel timezone-aware باشد.

---

### 🟡 B10 — سیگنال‌های بدون SL عملاً no-op می‌شوند
**محل**: `strategies/default_risk.py:46` (`no_sl_loss_pct=100`) + سایزینگ `simulator.py:294`.

استاپ مصنوعی ۱۰۰٪ → `stop_distance ≈ entry` → `qty = risk_amount/entry` بسیار کوچک → سهم ناچیز در PnL. عملاً سیگنال‌های بدون SL در نتیجه دیده نمی‌شوند.

**اثر**: کانال‌هایی که اغلب بدون SL سیگنال می‌دهند، عملکردشان کم‌نمایی می‌شود؛ ممکن است با هدف «هر سیگنال OPEN باید شبیه‌سازی شود» (که در validator آمده) در تضاد باشد.

**پیشنهاد**: یک `no_sl_loss_pct` واقع‌گرایانه‌تر (مثلاً ۵–۱۰٪) یا سایزینگ مبتنی بر notional ثابت برای سیگنال‌های بدون SL؛ تصمیم را صریح و قابل‌تنظیم کنید.

---

## مسائل استحکام و خطا (robustness)

### 🟡 B11 — fill در «کندل ورود» با کل دامنه‌ی همان کندل
**محل**: `simulator.py:629` (`if candle.open_time < position.entry_time: continue`).

کندل ورود (open_time == entry_time) پردازش می‌شود و کل high/low آن برای SL/TP استفاده می‌شود؛ در حالی‌که برای MARKET، entry در `open` همان کندل است و برای RANGE در midpoint. این می‌تواند در همان کندل ورود به TP/SL برخورد کند (به‌خصوص با midpoint).

**اثر**: خروج هم‌کندلِ ورود؛ کمی غیرواقعی، آمیخته با B5.

**پیشنهاد**: مستندسازی صریح؛ یا برای MARKET، شروع ارزیابی SL/TP را از کندل **بعدی** قرار دهید (محافظه‌کارانه‌تر).

### ⚪ W1 — `_select_classifier` ساخت تکراری `AjilGatewayClient`
**محل**: `real_runner.py:1066-1128`. دو بلوک تقریباً یکسان client می‌سازند. refactor به یک helper.

### 🟠 W2 — قاطی‌شدن گاردهای تست با گیتینگ production + side-effect در readiness
**محل**: `real_runner.py:296-329`.

برای اجرای یک بک‌تست واقعی باید `RUN_BACKTEST_INTEGRATION_TESTS=1`, `RUN_TELEGRAM_INTEGRATION_TESTS=1`, `RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1` ست شوند — این‌ها معنایی «گارد تست» دارند ولی اینجا برای اجرای واقعی لازم‌اند. همچنین `readiness()` (که یک query است) پوشه می‌سازد (side-effect).

**پیشنهاد**: یک پرچم runtime مجزا (مثلاً همان `REAL_BACKTEST_ENABLED`) کافی باشد؛ ساخت پوشه را به مسیر اجرای واقعی منتقل کنید نه به readiness.

### 🟡 W3 — بازگشت بی‌صدا به strategy پیش‌فرض هنگام خطای config — ✅ رفع شد
**محل**: `strategies/registry.py` (دو `except Exception`). اکنون هر دو `except` با `logging.warning(..., exc_info=True)` خطا را log می‌کنند قبل از fallback به default. `logger = logging.getLogger(__name__)` اضافه شد.

### 🟡 W4 — interval اشتباه در خلاصه‌ی تلگرام — ✅ رفع شد
**محل**: `report.py:29`، `domain/models.py:325`، `engine.py:125`.

**مشکل اصلی:** `interval = report.trades[0].notes[0]` — اولین note یک ترید (مثل `synthetic_stop=...`) را به‌اشتباه به‌عنوان interval نمایش می‌داد.

**رفع انجام‌شده:** فیلد `interval: str = "1m"` به `BacktestReport` اضافه شد (پیش‌فرض `"1m"` برای سازگاری با کد قدیمی)؛ در `engine.py` از `request.interval` پر می‌شود؛ `report_to_telegram_summary` اکنون مستقیماً از `report.interval` استفاده می‌کند.

### 🟡 W5 — اختلاط datetime naive/aware در candle cache
**محل**: `candle_cache.py:72 _as_utc`. خروجی گاهی naive و گاهی aware؛ مقایسه‌ها می‌توانند خطا/لغزش بدهند. **پیشنهاد**: یکدست aware-UTC نگه دارید.

### 🟡 W6 — سقف `MAX_CANDLES` اعمال نمی‌شود
**محل**: `settings.py:233,243`. `BACKTEST_MAX_CANDLES`/`REAL_BACKTEST_MAX_CANDLES` تعریف شده ولی در fetch/simulate جایی محدود نمی‌کند. **اثر**: روی بازه‌های بزرگ، مصرف حافظه نامحدود. **پیشنهاد**: اعمال سقف یا حذف تنظیمات گمراه‌کننده.

### 🟡 W7 — `detect_tp_list_update` می‌تواند اعداد نامرتبط را به‌عنوان TP بگیرد
**محل**: `directives.py:100`. هر پیام با مارکر «tp/تارگت/اهداف» و ≥۲ عدد، آن اعداد را TP ladder می‌گیرد (با `replace(",","")`). اعداد تاریخ/درصد/شناسه ممکن است اشتباه به‌عنوان قیمت گرفته شوند. **پیشنهاد**: فیلتر بازه‌ی قیمت معقول نسبت به entry/قیمت بازار؛ یا حذف اعداد با علامت `%`.

### ⚪ W8 — `profit_factor=None` در خروجی چاپ می‌شود — ✅ رفع شد
**محل**: `report.py:47`. اکنون: `metrics.profit_factor if metrics.profit_factor is not None else '∞'`

### ⚪ W9 — degrade بی‌صدای AI به ai_failed
**محل**: `real_runner.py:1456`. اگر گیت‌وی AI قطع شود، **همه‌ی** پیام‌ها `ai_failed` می‌شوند، هیچ ترید ساخته نمی‌شود، ولی run همچنان `success=True` با صفر سیگنال است (warning می‌خورد). تشخیص بین «کانال سیگنال نداشت» و «AI کلاً قطع بود» برای کاربر سخت می‌شود. **پیشنهاد**: اگر نرخ `ai_failed` از آستانه‌ای گذشت، run را failure با دلیل صریح علامت بزنید.

### 🟡 W10 (B10) — clamp نهایی `final_balance` به صفر، blow-up را پنهان می‌کند — ✅ نیمه‌رفع
**محل**: `engine.py:104-109`. اگر استراتژی حساب را منفی کند، `final_balance=max(...,0)` همچنان اعمال می‌شود (برای سازگاری با validatorِ `≥0`). **رفع انجام‌شده:** اکنون اگر `raw_final_balance < 0`، warningِ صریح `account_blown_up=true` به گزارش افزوده می‌شود تا blow-up پنهان نماند.

**باقی‌مانده (باز):** مقدار drawdown واقعی (بیش از ۱۰۰٪) همچنان clamp و گزارش نمی‌شود؛ پیشنهاد: یک متریک/پرچم `liquidated` با drawdown حقیقی.

---

## نقاط قوت (برای حفظ)

- جداسازی تمیز interfaceها (classifier، market data، telegram، strategy) → تست‌پذیری عالی.
- استفاده‌ی سراسری از `Decimal` برای مالی.
- ماژول `correlation.py` defensive و pure برای بی‌اعتمادی به AI — طراحی بالغ.
- پرچم‌های صداقت (`ai_used`, `real_market_data_used`, …) و warningهای صریح برای directiveهای attach‌نشده.
- گاردهای امنیتی محکم (live مسدود، فقط endpoint عمومی).
- پوشش تست گسترده (پوشه‌ی `tests/backtesting/` و `tests/dashboard/` ده‌ها فایل).

---

## اولویت‌بندی پیشنهادی رفع (موارد باقی‌مانده)

1. **B1** (کارایی O(N²)) — بیشترین اثر عملی روی runهای واقعی. (همراه B8)
2. **B3 (باقی‌مانده)** اسلیپیج/فاندینگ — واقع‌گرایی بیشتر PnL.
3. **B7 / B9** — واقع‌گرایی ریسک و drawdown زمانی.
4. **B5** — fill در midpoint بجای لبه برای RANGE entry.
5. بقیه‌ی W ها — استحکام و تجربه‌ی کاربری.

> ✅ رفع‌شده در این بازبینی: **B2، B3 (کارمزد)، B4، B6، B10**.

> پیش از هر تغییر: تست مربوطه را در `tests/backtesting/` اجرا و یک تست رگرسیون برای رفتار درست اضافه کنید (`pytest`, `ruff check .`, `mypy src` طبق `AGENTS.md`).
