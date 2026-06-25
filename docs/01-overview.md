# ۰۱ — نمای کلی پروژه و قابلیت‌ها

## هدف پروژه

`Triak_Trade` یک «پایه‌ی ماژولار هوش سیگنال» است که پیام‌های کانال‌های سیگنال تلگرام را دریافت، طبقه‌بندی و تحلیل می‌کند، روی داده‌ی تاریخی بازار **بک‌تست** می‌گیرد، و در فازهای بعدی دمو-تریدینگ/مانیتورینگ را پشتیبانی می‌کند. تصمیم نهایی پارس/طبقه‌بندی قرار است AI-محور باشد (Ajil Unified AI Gateway) و regex فقط fallback/safety است.

منابع مرجع داخل ریپو:
- `README.md` — راهنمای نصب و دستورات CLI.
- `AGENTS.md` — قوانین سخت‌گیرانه‌ی ایمنی/تست (مرجع اصلی «باید/نباید»های پروژه).

## پشته‌ی فناوری

- **زبان**: Python ≥ 3.10، typing سخت‌گیرانه (`mypy --strict`).
- **مدل‌ها/اعتبارسنجی**: Pydantic v2.
- **ذخیره‌سازی**: SQLAlchemy 2.0 + Alembic + MySQL (PyMySQL)، Redis برای کش.
- **HTTP**: httpx (async).
- **تلگرام**: Telethon (پشت interface).
- **CLI**: Typer + Rich.
- **داشبورد**: FastAPI + Jinja2 + WebSocket (local-first، بدون Node/React).
- **لاگ**: structlog + python-json-logger.

پیکربندی واحد از طریق `pydantic-settings` در `src/triak_trade/config/settings.py` و فایل ریشه‌ی `.env.local`.

## نقشه‌ی ماژول‌ها (`src/triak_trade/`)

| ماژول | نقش |
|-------|-----|
| `domain/` | مدل‌های دامنه (Pydantic) و enumها: `ParsedSignal`, `Candle`, `SimulatedTrade`, `SignalState`, `BacktestReport`, `ChannelMetrics` و … |
| `parsing/` | normalizer + regex parser + validator — تبدیل متن خام به `ParsedSignal`. |
| `agents/` | لایه‌ی classifier (پروتکل `MessageClassifier`)، `ChannelContext` (وضعیت per-channel)، `RegexMessageClassifier`. |
| `ai/` | کلاینت Ajil Gateway، `AIMessageClassifier`، schemas و prompts. |
| `market_data/` | تأمین‌کننده‌های کندل: Binance public (آرشیو + REST)، Toobit، Composite (fallback)، Candle cache، factory، intervals. |
| `backtesting/` | **هسته‌ی بک‌تست** — موضوع اصلی این مستندات. |
| `telegram/` | کلاینت Telethon، history sync، live listener، mapper. |
| `exchange/toobit/` | امضاکننده، اکانت، spot، futures، demo execution، safety. |
| `dashboard/` | UI کنترل محلی، runtime بک‌تست زنده، workspace دمو-تریدینگ، routes، realtime/WebSocket. |
| `admin_bot/` | ربات تأیید ادمین (approve/reject/watch؛ بدون اجرای معامله). |
| `observability/` | event bus، processing audit، کانال لاگ تلگرام، redaction. |
| `verification/` | اجراکننده‌ی verify-system و گزارش‌های سلامت. |
| `core/` | logging، health، symbols (نرمال‌سازی نماد)، time (TZ تهران)، errors. |
| `db/` | engine، session، models، repositories. |
| `deployment/` | bootstrap گیت‌وی Ajil و runtime env. |

## قابلیت‌های اصلی (از منظر کاربر)

دستورات CLI کلیدی (از `README.md` و `src/triak_trade/cli.py`):

- **بک‌تست fixture**: `triak-trade backtest-fixture` — اجرای قطعی روی داده‌ی نمونه.
- **بک‌تست واقعی**:
  - `real-backtest-check` — نمایش readiness بدون افشای راز.
  - `real-backtest-run --channel … --hours … --interval …`
  - `real-backtest-tofan --hours …` — کانال پیش‌فرض پیکربندی‌شده.
  - `backtest-show-latest` — آخرین گزارش ذخیره‌شده.
- **داشبورد**: اجرای بک‌تست زنده با progress لحظه‌ای، نمودار سیگنال، live metrics، امکان stop/rerun، و workspace دمو-تریدینگ چند-session.
- پارس/طبقه‌بندی پیام، بررسی گیت‌وی AI، تاریخچه‌ی تلگرام، داده‌ی بازار، ربات ادمین، کانال لاگ، verify-system.

## اصول طراحی غیرقابل‌مذاکره (از `AGENTS.md`)

1. هر ماژول مستقلاً قابل‌تست؛ سرویس بیرونی پشت interface.
2. تست‌های unit هرگز سرویس واقعی صدا نمی‌زنند؛ تست‌های integration پشت گارد env.
3. هیچ سفارش واقعی/برداشت واقعی؛ workspace دمو-تریدینگ فعلاً demo-only است و live order execution همچنان مسدود می‌ماند.
4. همه‌ی مقادیر مالی `Decimal`.
5. هرگز چاپ/کامیت راز.
6. بک‌تست simulation-only؛ از endpointهای خصوصی استفاده نمی‌کند.
7. بک‌تست باید از interface طبقه‌بندی (AI-ready) استفاده کند، نه مستقیم از regex.
8. اگر AI در دسترس نبود، صریحاً گزارش شود؛ هرگز وانمود نشود AI استفاده شده.

این اصول مستقیماً معماری بک‌تست را شکل داده‌اند (در فایل‌های بعدی توضیح داده می‌شود).
