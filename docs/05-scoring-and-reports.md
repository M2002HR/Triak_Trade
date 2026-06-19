# ۰۵ — امتیازدهی کانال، متریک‌ها و گزارش‌ها

## متریک‌ها — `ChannelScorer.score_with_breakdown` (`scoring.py:83`)

از روی events و trades محاسبه می‌شود:

| متریک | محاسبه |
|-------|--------|
| `parsed_signals` | تعداد eventهای OPEN |
| `ignored` / `invalid` | eventهای IGNORE / UNKNOWN |
| `wins` | `t.pnl > 0` روی **همه‌ی** تریدها |
| `filled_trades` | `status != "not_filled"` |
| `gross_win/loss` | جمع pnl مثبت/منفی روی filled |
| `win_rate` | `wins / len(trades)` ← **مخرج: همه‌ی تریدها (شامل not_filled و breakeven)** |
| `profit_factor` | `gross_win / gross_loss` یا None اگر loss صفر |
| `expectancy` | `total_pnl / len(trades)` |
| `max_drawdown` | بیشینه افت equity تجمعی (`_max_drawdown`) |

> ⚠️ ناهماهنگی مخرج: `win_rate` روی همه‌ی تریدها ولی `profit_factor`/wins-losses در breakdown روی filled. این win_rate را به‌طور سیستماتیک پایین می‌آورد (فایل ۰۸ **B6**).

> ⚠️ `_max_drawdown` (`scoring.py:216`) روی **ترتیب لیست تریدها** equity می‌سازد، نه ترتیب زمانی `exit_time`؛ و فقط PnLِ realized بسته‌شده را می‌بیند (drawdown داخل-ترید/unrealized نادیده) (فایل ۰۸ B9).

## امتیاز نهایی — `build_score_breakdown` (`scoring.py:133`)

هفت زیرامتیاز (۰..۱۰۰) با وزن:

| زیرامتیاز | وزن | تعریف |
|-----------|-----|-------|
| profitability | 0.24 | `return_pct` نسبت به سقف ۳۰٪ |
| win_rate | 0.18 | `win_rate * 100` |
| profit_factor | 0.17 | نسبت به سقف ۳.۰ (یا ۱۰۰ اگر loss صفر و win>0) |
| drawdown_control | 0.15 | `1 - min(dd_pct/0.20, 1)` |
| fill_rate | 0.10 | `filled / parsed_signals` |
| consistency | 0.10 | `1 - |optimistic-conservative| / ref` |
| sample_confidence | 0.06 | `min(filled/12, 1)` |

سپس:
```
weighted              = Σ(score_i * weight_i)
confidence_multiplier = 0.70 + sample_confidence * 0.30
final_score           = clamp(weighted * confidence_multiplier, 0, 100)
```

- `return_pct = total_pnl / initial_balance`، `drawdown_pct = max_drawdown / initial_balance`.
- ضریب confidence نمونه‌ی کوچک را جریمه می‌کند (با ۱۲ ترید پر اشباع می‌شود — عدد سحرآمیز ثابت).

تصمیم طراحی: امتیاز چند-بُعدی است تا یک کانال صرفاً پرسود ولی پرریسک/کم‌نمونه امتیاز کامل نگیرد. این منطقی است، ولی پارامترها (سقف ۳۰٪، ۲۰٪ DD، ۱۲ نمونه، وزن‌ها) hard-coded و بدون calibration تجربی‌اند.

## گزارش‌ها — `report.py`

- `report_to_json` (`report.py:13`): dump کامل + `channel_score` + `score_breakdown` + `trade_status_counts` + `symbol_summary` + `equity_curve`.
- `report_to_telegram_summary`: خلاصه‌ی انسانی (نکته: `interval` را به‌اشتباه از `trades[0].notes[0]` می‌خواند که note است نه interval — فایل ۰۸ W4).
- `report_to_markdown_summary`: نسخه‌ی Markdown.
- `extract_channel_score`: پارس امتیاز از `warnings` (الگوی `channel_score=…`).
- `_equity_curve`: روی **ترتیب لیست تریدها** equity تجمعی می‌سازد (همان مشکل ترتیب زمانی).

## ذخیره‌سازی — `report_store.py`

`BacktestReportStore.write` دو فایل می‌نویسد: `real_backtest_{slug}_{stamp}.report.json` و `.report.md` زیر `runtime/reports/backtests/`. `latest()` و `list_reports()` بر اساس mtime مرتب می‌کنند. payload شامل پرچم‌های صداقت (`ai_used`, `real_market_data_used`, …) و `score_reason` است.

## correlation follow-up — `correlation.py`

`resolve_related_signal_id` (`correlation.py:83`) منطق قطعی attach کردن follow-up به سیگنال درست:

1. **AI id** فقط اگر به سیگنال زنده map شود (idهای نامعتبر مثل `""`, `"unknown"`, عدد خام تلگرام رد می‌شوند — `is_invalid_ai_related_id`).
2. **reply chain**: مالک پیامِ reply‌شده.
3. **تطبیق نماد**: تک‌سیگنال هم‌نماد، وگرنه جدیدترین.
4. **تک‌سیگنال باز**: برای directiveهای روشن (close/cancel/update) بدون نماد.
5. **last resort** (پشت `REAL_BACKTEST_FOLLOWUP_LAST_RESORT_ATTACH`): جدیدترین سیگنال باز در حالت مبهم چند-سیگنالی.

این ماژول pure است (context را تغییر نمی‌دهد) و method/note برای trace برمی‌گرداند. طراحی خوب و defensive؛ پاسخ مستقیم به بی‌اعتمادی به خروجی AI.
