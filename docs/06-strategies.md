# ۰۶ — استراتژی‌های مدیریت معامله (`strategies/`)

استراتژی‌ها مجموعه قوانین **بدون حالت** (stateless) برای مدیریت پوزیشن‌اند و قابل‌استفاده در بک‌تست و (در آینده) اجرای واقعی. سه مسئولیت دارند:

1. استاپ مصنوعی وقتی سیگنال SL ندارد.
2. نردبان TP مصنوعی وقتی TP ندارد.
3. تصمیم partial-close و جابه‌جایی استاپ هنگام برخورد هر TP.

## پروتکل — `TradeStrategy` (`strategies/base.py:27`)

`@runtime_checkable Protocol` با `name` و سه متد:
- `get_synthetic_stop(side, entry_price) -> Decimal`
- `get_synthetic_take_profits(side, entry_price, stop_loss) -> list[Decimal]`
- `get_target_hit_action(targets_hit_so_far, remaining_targets_including_this, entry_price, take_profits) -> TargetHitAction`

`TargetHitAction` (`base.py:12`): `close_fraction`, `move_sl_to_entry`, `new_stop_loss`.

## `DefaultRiskManagedStrategy` (`strategies/default_risk.py`)

پارامترها:
- `no_sl_loss_pct = 100` — استاپ مصنوعی ۱۰۰٪ دور از entry.
  - long: `entry*(1-1)=0` (عملاً استاپ بسیار دور؛ قیمت کریپتو به صفر نمی‌رسد).
  - short: `entry*2`.
- `risk_free_on_first_tp = True` — بعد از TP1، استاپ به entry منتقل (breakeven).
- `tp_close_fractions = [0.35, 0.40, 0.50]` — کسر بستن در هر TP (آخرین تکرار می‌شود؛ TP نهایی همیشه ۱۰۰٪).
- `synthetic_tp_r_multiples = [1, 2, 3]` — نردبان R-multiple برای TP مصنوعی.

> ⚠️ پیامد طراحی: با `no_sl_loss_pct=100`، سیگنال‌های **بدون SL** استاپ بسیار دور می‌گیرند → `stop_distance ≈ entry` → `qty = risk_amount/entry` بسیار کوچک → سهم این سیگنال‌ها در PnL تقریباً صفر. یعنی سیگنال‌های بدون SL عملاً no-op می‌شوند و عملکرد کانال را کم‌نمایی می‌کنند (فایل ۰۸ B10).

`get_target_hit_action`:
- TP نهایی → بستن ۱۰۰٪ + (در TP1) breakeven.
- وگرنه کسر از `tp_close_fractions[min(targets_hit, len-1)]`، clamp به (0.01, 0.99).

## `TrailingTakeProfitStrategy` (`strategies/trailing_tp.py`)

ارث‌بری از Default + trailing استاپ روی نردبان TP:
- بعد از TP2 → استاپ به TP1، بعد از TP3 → استاپ به TP2، و…
- `get_target_hit_action` ابتدا base را می‌گیرد، سپس `new_stop_loss = take_profits[targets_hit-1]`.

## رجیستری و بارگذاری — `strategies/registry.py`

- `_STRATEGY_CLASSES`: `default_risk_managed`, `tp_trailing_risk_managed`.
- `load_strategy(config_path=None)`: از `config/strategies.yaml` (project root = `parents[4]`)؛ اگر فایل نبود/خراب بود/`pyyaml` نبود → defaults.
- `load_strategy_from_dict`, `build_strategy_from_key` (برای انتخاب از داشبورد), `list_available_strategies` (با پارامترهای serialize‌شده).

> نکته‌ی استحکام: همه‌ی خطاهای بارگذاری config با `except Exception` بی‌صدا به default برمی‌گردند — اگر کاربر strategies.yaml را اشتباه بنویسد، بی‌هیچ هشداری نسخه‌ی پیش‌فرض اجرا می‌شود (فایل ۰۸ W3).

## تصمیم طراحی کلیدی

استراتژی‌ها عمداً از منطق شبیه‌ساز جدا شده‌اند تا همان قوانین بعداً در اجرای دمو/واقعی هم استفاده شوند. این جداسازی تمیز و قابل‌تست است؛ نقطه‌ی قوت معماری.
