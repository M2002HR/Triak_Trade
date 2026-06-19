# ۰۴ — خط لوله‌ی بک‌تست واقعی (`real_runner.py`)

`RealBacktestRunner` (۳۰۹۹ خط) کامل‌ترین و حساس‌ترین بخش است: تاریخچه‌ی واقعی تلگرام را می‌گیرد، با AI (یا regex) طبقه‌بندی می‌کند، کندل واقعی می‌آورد، شبیه‌سازی می‌کند، trace هر پیام و live state می‌سازد، و گزارش ذخیره می‌کند.

## وابستگی‌های تزریق‌پذیر (سازنده `real_runner.py:263`)

- `telegram_client` → `TelethonTelegramClient` (پشت `TelegramClientInterface`).
- `market_data_provider` → `build_backtest_market_data_provider(settings)` (Binance public + fallback Toobit).
- `report_store` → `BacktestReportStore`.
- `log_client` → `TelegramLogChannelClient`.
- `strategy` → از `config/strategies.yaml` یا تزریق مستقیم.
- `validator` → `ParsedSignalValidator`.

همه fakeپذیر؛ تست‌های unit بدون شبکه.

## readiness (`real_runner.py:296`)

قبل از اجرا، شرایط لازم چک می‌شود و اگر کامل نبود، اجرا **بلاک** و گزارش failure نوشته می‌شود. شروط:
- `REAL_BACKTEST_ENABLED=true`
- `RUN_BACKTEST_INTEGRATION_TESTS=1`, `RUN_TELEGRAM_INTEGRATION_TESTS=1`, `RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1`
- اعتبارنامه‌ی تلگرام (`TELEGRAM_API_ID/HASH`) و session پیکربندی‌شده.
- تنظیمات داده‌ی تاریخی Binance کامل.

> ⚠️ این متد به‌عنوان side-effect پوشه می‌سازد و **گاردهای تست** را با گیتینگ production مخلوط می‌کند (فایل ۰۸ W2).

## مراحل `run(...)` (`real_runner.py:346`)

ترتیب فازها (با `progress_callback` به داشبورد گزارش می‌شوند):

1. **starting / readiness** — اگر ناموفق → failure.
2. **انتخاب classifier** (`_select_classifier`, `real_runner.py:1065`):
   - `use_ai && AI_GATEWAY_ENABLED` → `AIMessageClassifier` با `AjilGatewayClient`.
   - `use_ai && !enabled` → failure («AI لازم است ولی فعال نیست»).
   - else → `RegexMessageClassifier`.
3. **fetch_history** — `BacktestTelegramSource.fetch` (محدود به `min(max_messages, REAL_BACKTEST_MAX_MESSAGES)`). اگر `start_message_id` داده شود، بازه‌ی تاریخ نادیده گرفته و از آن پیام به بعد گرفته می‌شود.
4. **classify_messages** — `_build_events_with_traces` (قلب پیچیدگی؛ پایین‌تر).
5. استخراج `symbols` از سیگنال‌های OPEN معتبر؛ اگر هیچ پیام/سیگنال/کندلی نبود → failure دقیق.
6. **fetch_market_data** — برای هر نماد کندل می‌گیرد (با candidateهای نماد و fallback). از کندل‌های pre-fetch‌شده در فاز classify دوباره استفاده می‌کند.
7. **simulate** — `engine.run_from_events(...)` با `active_signal_hours`, `max_effective_leverage`, `default_stop_pct`.
8. به‌روزرسانی trace هر سیگنال با نتیجه‌ی ترید.
9. ساخت `RealBacktestResult`، نوشتن گزارش JSON+MD، ارسال خلاصه به کانال لاگ.

`run_sync` همان را با `asyncio.run` در thread اجرا می‌کند (برای داشبورد).

## `_build_events_with_traces` (`real_runner.py:1301`) — هسته‌ی هوشمندی

برای هر پیام (به ترتیب تاریخ):

1. ساخت `trace` با ۷ مرحله: received → preprocess → classified → validated → market_data → simulated → finalized.
2. **preprocess**: دانلود on-demand مدیا (کپشن‌دار)، و الصاق مدیای بدون‌کپشن قبلی به‌عنوان context تصویری (`_maybe_attach_prior_captionless_media`) — برای پیام‌هایی که متن سیگنال‌گونه دارند ولی عکسشان جدا آمده.
3. متن خالی → `IGNORE`.
4. `classifier.classify(...)` در try/except: اگر استثنا → شمارش `ai_failed_messages`، ثبت `IGNORE` و **ادامه** (یک پیام بد نباید کل اجرا را خراب کند).
5. **بازمسیریابی (rerouting) هوشمند**:
   - اگر AI پیام را OPEN جدید دانست ولی reply به پیامی است که صاحب سیگنال زنده است → تبدیل به follow-up (`real_runner.py:1534`).
   - اگر OPEN جدید روی نمادی است که سیگنال باز معتبری دارد → بازمسیریابی به `UPDATE_TP`/`UPDATE_ENTRY` (`_find_reusable_open_signal_for_symbol`).
6. **سیگنال جدید**: `make_signal_id` + ثبت در `ChannelContext` (pending).
7. **follow-up**:
   - تعیین `effective_action` با `normalize_related_signal_action` + `apply_text_directive_action` (متن صریح breakeven/close).
   - بازیابی «Tp List» با `detect_tp_list_update` (اگر AI آن را ambiguous کرد).
   - **promote reply parent** (`_maybe_promote_reply_parent`, `real_runner.py:1945`): اگر reply به پیامی است که خودش سیگنال باز نشده ولی parse می‌شود به OPEN، آن parent به‌صورت گذشته‌نگر ثبت و یک رویداد OPEN در timestamp آن تزریق می‌شود (سناریوی msg /6285).
   - **correlation قطعی** با `resolve_related_signal_id` (فایل ۰۲/۰۵): AI id معتبر → reply chain → تطبیق نماد → تک‌سیگنال باز → last resort.
   - اگر directive روشن ولی attach نشد → warning صریح (هرگز بی‌صدا گم نمی‌شود).
8. **validate + pre-fetch market data**: برای OPENهای معتبر، کندل بلافاصله pre-fetch می‌شود تا شبیه‌سازی زنده ممکن شود.
9. ثبت `BacktestEvent` و به‌روزرسانی شمارنده‌ها.
10. **`_update_live_simulation_state`** بعد از هر پیام.

## شبیه‌سازی زنده — `_update_live_simulation_state` (`real_runner.py:2279`)

```python
simulator.simulate_with_snapshots(
    events=events,                     # همه‌ی رویدادهای تا الان
    candles=available_candles,         # همه‌ی کندل‌های pre-fetch‌شده
    ... snapshot_interval=refresh_interval)
```

از آخرین snapshot، live_metrics و live_signals (با نمودار، lifecycle، تاریخچه‌ی SL/TP) ساخته و به داشبورد emit می‌شود.

> ⚠️⚠️ این تابع **بعد از هر پیام** فراخوانی می‌شود و **کل شبیه‌سازی را از صفر** روی تمام رویدادها و تمام کندل‌ها بازاجرا می‌کند → پیچیدگی O(N²·M). این مهم‌ترین مشکل کارایی است (فایل ۰۸ **B1**). علاوه بر آن `_emit_interval_snapshots` همه‌ی snapshotهای interval را هر بار دوباره emit می‌کند.

## یکپارچگی داشبورد (`dashboard/backtest_runtime.py`)

- `DashboardBacktestCoordinator.start_run` یک thread daemon می‌سازد که `runner.run_sync` را با progress_callback اجرا می‌کند.
- `_handle_progress` هر event را در فایل JSON کل run می‌نویسد (`store.write`) و notify می‌کند → **هر event = بازنویسی کامل فایل** (فایل ۰۸ B8).
- cancellation: پرچم در `_cancel_requested`؛ فقط داخل callback چک می‌شود (`DashboardBacktestCancelledError`).
- `_recover_incomplete_runs` runهای ناتمام را در شروع به failed علامت می‌زند.
- merge سیگنال‌ها و متریک‌های تجمعی (`_refresh_signal_aggregate_metrics`).

## خروجی — `RealBacktestResult` (`real_runner.py:216`)

شامل پرچم‌های صداقت: `real_telegram_used, real_market_data_used, ai_used, regex_fallback_used`, شمارنده‌های پیام/سیگنال، `ai_failed_messages`, متریک‌های مالی، `skipped_reasons`, `errors`, `warnings`, و مسیر گزارش‌ها. این پرچم‌ها تضمین می‌کنند هرگز وانمود نشود AI/داده‌ی واقعی استفاده شده (طبق `AGENTS.md`).
