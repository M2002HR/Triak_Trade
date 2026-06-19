# ۰۸ — باگ‌ها، نقاط ضعف و پیشنهادهای بهبود

> این فایل حساس‌ترین بخش بازبینی است. هر مورد با **شناسه، شدت، محل دقیق، توضیح، و پیشنهاد رفع** آمده.
> شدت‌ها: 🔴 بحرانی · 🟠 مهم · 🟡 متوسط · ⚪ جزئی/بهبود.
>
> هیچ کدی در این بازبینی تغییر داده نشده است؛ این موارد پیشنهادی‌اند و باید با تست تأیید شوند.

---

## باگ‌های صحت محاسبات مالی (مهم‌ترین دسته)

### 🔴 B1 — شبیه‌سازی زنده O(N²): بازاجرای کامل به‌ازای هر پیام
**محل**: `real_runner.py:2279 _update_live_simulation_state`، فراخوانی در `:1434`, `:1890` و promote.

`_update_live_simulation_state` بعد از **هر** پیام، `simulator.simulate_with_snapshots(...)` را روی **تمام** رویدادها و **تمام** کندل‌های جمع‌شده از صفر اجرا می‌کند. برای N پیام و M کندل، هزینه ~ O(N²·M). با `REAL_BACKTEST_MAX_MESSAGES=1000` و کندل‌های ۱m روی ۲۴ ساعت چند نماد، این یعنی صدها بازاجرای کامل + emit رویداد. علاوه بر این:
- `_emit_interval_snapshots` (`:2346`) **همه‌ی** snapshotهای interval را هر بار دوباره emit می‌کند (نه فقط جدیدها) → انفجار رویداد.
- هر emit در داشبورد منجر به یک بازنویسی کامل فایل JSON می‌شود (B8).

**اثر**: کندی شدید، مصرف CPU/I/O بالا، احتمال timeout روی runهای واقعی بزرگ.

**پیشنهاد**:
- شبیه‌سازی زنده را incremental کنید: وضعیت پوزیشن‌ها را نگه دارید و فقط کندل/رویداد جدید را اعمال کنید؛ یا
- live update را throttle کنید (مثلاً هر K پیام یا هر T ثانیه)، یا فقط روی delta رویدادها.
- `_emit_interval_snapshots` فقط snapshotهای جدید نسبت به آخرین emit را بفرستد (یک cursor نگه دارید).

---

### 🔴 B2 — ناهماهنگی conservative/optimistic بین `total_pnl` و لیست تریدها
**محل**: `engine.py:85-128`.

`report.trades` همیشه `conservative_trades` است، ولی:
```python
final_balance = max(optimistic_final if fill_policy==OPTIMISTIC else conservative_final, 0)
total_pnl = final_balance - initial_balance
```
وقتی `fill_policy=OPTIMISTIC`، `total_pnl` از optimistic می‌آید ولی `win_rate`, `profit_factor`, `max_drawdown`, equity curve همگی از conservative_trades محاسبه می‌شوند. در نتیجه:
- `sum(trade.pnl for trade in report.trades) ≠ total_pnl`.
- `expectancy = total_pnl / len(trades)` با تریدهای ناهمخوان.
- equity curve به final_balance نمی‌رسد.

**اثر**: گزارش از درون ناسازگار؛ امتیاز و متریک‌ها قابل‌اتکا نیستند در حالت optimistic.

**پیشنهاد**: متریک‌ها و total_pnl را از **همان** مجموعه‌ی تریدِ منتخب fill_policy بسازید. یا هر دو مجموعه را جدا گزارش کنید و هیچ‌گاه آن‌ها را mix نکنید.

---

### 🔴 B3 — کارمزد، اسلیپیج و فاندینگ مدل نشده
**محل**: `simulator.py:845 _close_fraction_of_position`، فیلد `realized_fees`.

`realized_fees` هرگز افزایش نمی‌یابد (`_calculate_pnl` فقط قیمت*مقدار است). `_finalize_position` فقط `max(realized_fees, 0)=0` را گزارش می‌کند. هیچ کارمزد taker/maker، اسلیپیج، یا funding rate (برای فیوچرز ۲۴ساعته) لحاظ نمی‌شود.

**اثر**: PnL سیستماتیک **خوش‌بینانه**؛ برای استراتژی‌های پر-معامله/اهرم‌بالا اختلاف می‌تواند زیاد باشد. امتیاز کانال بیش‌برآورد می‌شود.

**پیشنهاد**:
- پارامتر `fee_rate` (و اختیاری `slippage_bps`, `funding_rate`) به شبیه‌ساز اضافه کنید.
- در هر fill: `fee = exit_notional * fee_rate` (و entry fee)، به `realized_fees` و در PnL کسر شود.
- funding بر حسب مدت نگهداری × نرخ برای پوزیشن‌های فیوچرز.

---

### 🟠 B4 — تطبیق نماد ناهمگون (`==` به‌جای `same_market_symbol`)
**محل**: `simulator.py:580 _first_candle_open_after` (`c.symbol == symbol`).

بقیه‌ی کد از `same_market_symbol`/`canonical_market_symbol` استفاده می‌کند، ولی این تابع تطبیق دقیق رشته‌ای دارد. وقتی `position.symbol` فرمت متفاوتی از `candle.symbol` دارد (مثلاً پس از `selected_symbol` که در real_runner ممکن است `BTCUSDT_PERP` و کندل `BTCUSDT` باشد)، lookup شکست می‌خورد و close دستی/close_all به `entry_price` fallback می‌کند → PnL صفر اشتباه برای آن خروج.

**اثر**: بستن دستی/CLOSE/close_all ممکن است به‌جای قیمت واقعی، breakeven ثبت کند.

**پیشنهاد**: از `same_market_symbol(c.symbol, symbol)` استفاده کنید (مطابق `_last_price` و `_find_entry_execution`).

---

### 🟠 B5 — fill در midpoint برای entry بازه‌ای (look-ahead خفیف)
**محل**: `simulator.py:557-564`.

برای `RANGE`، به‌محض اینکه کندلی بازه را لمس کند، فیل در **midpoint** بازه انجام می‌شود — صرف‌نظر از اینکه قیمت از کدام سمت وارد بازه شده. اگر قیمت فقط لبه‌ی بازه را لمس کرده باشد، midpoint قیمتی است که شاید هرگز معامله نشده.

**اثر**: entry خوش‌بینانه/بدبینانه بسته به جهت؛ سوگیری کوچک ولی سیستماتیک.

**پیشنهاد**: فیل در نزدیک‌ترین لبه‌ی بازه به قیمت ورودِ کندل (یا لبه‌ای که اول لمس می‌شود)، یا حداقل مستندسازی صریح این فرض به‌عنوان حالت optimistic.

---

### 🟠 B6 — مخرج `win_rate` شامل تریدهای پر-نشده و breakeven
**محل**: `scoring.py:103` (`wins / len(trades)`).

`len(trades)` شامل `not_filled` و تریدهای breakeven (pnl==0، که نه win نه loss شمرده می‌شوند) است. در حالی‌که `profit_factor`/`fill_rate` روی `filled_trades` کار می‌کنند. با استراتژی risk-free که SL را به entry می‌برد، تریدهای breakeven فراوان‌اند و `win_rate` را به‌طور مصنوعی پایین می‌کشند.

**اثر**: `win_rate` و در نتیجه `win_rate_score` (وزن ۰.۱۸) سیستماتیک کم‌برآورد؛ امتیاز کانال ناعادلانه پایین.

**پیشنهاد**: `win_rate = wins / filled_count` (یا حداقل حذف not_filled از مخرج)، و تصمیم صریح برای breakeven (شمارش جدا).

---

### 🟠 B7 — نبود سقف اکسپوژر/margin تجمعی بین پوزیشن‌های هم‌زمان
**محل**: `simulator.py:294-319` (سایزینگ).

هر OPEN مستقل با `balance` لحظه‌ای (فقط realized) سایز می‌شود. اگر چند سیگنال هم‌زمان باز باشند، مجموع margin مصرفی می‌تواند از کل balance بیشتر شود؛ هیچ بررسی margin پرتفویی نیست. همچنین balance برای سایزینگ شامل unrealized پوزیشن‌های باز نیست.

**اثر**: اکسپوژر غیرواقعی (اهرم مؤثر کل > سقف)، PnL غیرقابل‌بازتولید در حساب واقعی.

**پیشنهاد**: یک حساب margin مشترک نگه دارید؛ قبل از باز کردن، margin آزاد را چک کنید و در صورت کمبود رد/کاهش دهید.

---

### 🟡 B8 — بازنویسی کامل فایل JSON به‌ازای هر event داشبورد
**محل**: `dashboard/backtest_runtime.py:371 _handle_progress` → `store.write(run)`.

هر progress event کل state run (شامل لیست `messages` با `full_text` همه‌ی پیام‌ها و `events`) را serialize و روی دیسک بازنویسی می‌کند. همراه با B1 (انفجار رویداد)، I/O به‌شدت بالا می‌رود. `events` به ۲۵۰ محدود شده ولی `messages` و `signals` رشد می‌کنند.

**پیشنهاد**: نوشتن را throttle/debounce کنید (مثلاً هر ۲۵۰ms یا در پایان batch)؛ یا فقط delta را در یک append-log بنویسید و snapshot کامل را دوره‌ای ذخیره کنید.

---

### 🟡 B9 — drawdown و equity curve بر اساس ترتیب لیست تریدها، نه زمان
**محل**: `scoring.py:216 _max_drawdown`، `report.py:140 _equity_curve`.

تریدها به ترتیب «resolution» append می‌شوند (ابتدا تریدهای حل‌شده در پردازش کندل، بعد no-fill/eventها، در پایان open-until-end). این لزوماً ترتیب `exit_time` نیست. drawdown و equity curve روی این ترتیب ساخته می‌شوند.

**اثر**: max_drawdown و منحنی equity ممکن است از نظر زمانی نادرست باشند؛ drawdown داخل-ترید (unrealized) اصلاً دیده نمی‌شود.

**پیشنهاد**: تریدها را قبل از محاسبه بر اساس `exit_time` (یا entry_time) مرتب کنید؛ برای drawdown دقیق‌تر از equity مبتنی بر snapshot (که از قبل وجود دارد) استفاده کنید.

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

### 🟡 W3 — بازگشت بی‌صدا به strategy پیش‌فرض هنگام خطای config
**محل**: `strategies/registry.py:111, 193` (`except Exception`). اگر `strategies.yaml` خراب باشد، بی‌هیچ warning نسخه‌ی پیش‌فرض اجرا می‌شود. **پیشنهاد**: حداقل log/warning بدهید.

### 🟡 W4 — interval اشتباه در خلاصه‌ی تلگرام
**محل**: `report.py:30` — `interval` را از `trades[0].notes[0]` می‌خواند که یک note (مثل synthetic_stop) است، نه interval. **پیشنهاد**: interval را از request به report منتقل و مستقیم استفاده کنید.

### 🟡 W5 — اختلاط datetime naive/aware در candle cache
**محل**: `candle_cache.py:72 _as_utc`. خروجی گاهی naive و گاهی aware؛ مقایسه‌ها می‌توانند خطا/لغزش بدهند. **پیشنهاد**: یکدست aware-UTC نگه دارید.

### 🟡 W6 — سقف `MAX_CANDLES` اعمال نمی‌شود
**محل**: `settings.py:233,243`. `BACKTEST_MAX_CANDLES`/`REAL_BACKTEST_MAX_CANDLES` تعریف شده ولی در fetch/simulate جایی محدود نمی‌کند. **اثر**: روی بازه‌های بزرگ، مصرف حافظه نامحدود. **پیشنهاد**: اعمال سقف یا حذف تنظیمات گمراه‌کننده.

### 🟡 W7 — `detect_tp_list_update` می‌تواند اعداد نامرتبط را به‌عنوان TP بگیرد
**محل**: `directives.py:100`. هر پیام با مارکر «tp/تارگت/اهداف» و ≥۲ عدد، آن اعداد را TP ladder می‌گیرد (با `replace(",","")`). اعداد تاریخ/درصد/شناسه ممکن است اشتباه به‌عنوان قیمت گرفته شوند. **پیشنهاد**: فیلتر بازه‌ی قیمت معقول نسبت به entry/قیمت بازار؛ یا حذف اعداد با علامت `%`.

### ⚪ W8 — `profit_factor=None` در خروجی چاپ می‌شود
**محل**: `report.py:47`. وقتی هیچ ضرری نیست، «Profit factor: None» نمایش داده می‌شود. **پیشنهاد**: «∞» یا «N/A».

### ⚪ W9 — degrade بی‌صدای AI به ai_failed
**محل**: `real_runner.py:1456`. اگر گیت‌وی AI قطع شود، **همه‌ی** پیام‌ها `ai_failed` می‌شوند، هیچ ترید ساخته نمی‌شود، ولی run همچنان `success=True` با صفر سیگنال است (warning می‌خورد). تشخیص بین «کانال سیگنال نداشت» و «AI کلاً قطع بود» برای کاربر سخت می‌شود. **پیشنهاد**: اگر نرخ `ai_failed` از آستانه‌ای گذشت، run را failure با دلیل صریح علامت بزنید.

### ⚪ W10 — clamp نهایی `final_balance` به صفر، blow-up را پنهان می‌کند
**محل**: `engine.py:96`. اگر استراتژی حساب را منفی کند، `final_balance=max(...,0)` و گزارش معتبر می‌ماند (validator نیاز به `≥0` دارد). drawdown بیش از ۱۰۰٪ پنهان می‌شود. **پیشنهاد**: یک پرچم `liquidated/blown_up` و گزارش drawdown واقعی.

---

## نقاط قوت (برای حفظ)

- جداسازی تمیز interfaceها (classifier، market data، telegram، strategy) → تست‌پذیری عالی.
- استفاده‌ی سراسری از `Decimal` برای مالی.
- ماژول `correlation.py` defensive و pure برای بی‌اعتمادی به AI — طراحی بالغ.
- پرچم‌های صداقت (`ai_used`, `real_market_data_used`, …) و warningهای صریح برای directiveهای attach‌نشده.
- گاردهای امنیتی محکم (live مسدود، فقط endpoint عمومی).
- پوشش تست گسترده (پوشه‌ی `tests/backtesting/` و `tests/dashboard/` ده‌ها فایل).

---

## اولویت‌بندی پیشنهادی رفع

1. **B1** (کارایی O(N²)) — بیشترین اثر عملی روی runهای واقعی.
2. **B2 + B6** (ناهماهنگی متریک‌ها و win_rate) — اعتبار گزارش/امتیاز.
3. **B3** (کارمزد/اسلیپیج) — واقع‌گرایی PnL.
4. **B4** (تطبیق نماد در close) — صحت خروج‌های دستی.
5. **B7 / B9 / B10** — واقع‌گرایی ریسک و drawdown.
6. بقیه‌ی W ها — استحکام و تجربه‌ی کاربری.

> پیش از هر تغییر: تست مربوطه را در `tests/backtesting/` اجرا و یک تست رگرسیون برای رفتار درست اضافه کنید (`pytest`, `ruff check .`, `mypy src` طبق `AGENTS.md`).
