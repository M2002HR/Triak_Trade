"""Persian-friendly admin bot menus."""

from __future__ import annotations

from typing import Any

UNAUTHORIZED_TEXT = "⛔️ شما مجاز به استفاده از این ربات نیستید."
WELCOME_TEXT = (
    "به پنل مدیریت Triak_Trade خوش آمدید.\n"
    "این ربات فقط عملیات امن، بررسی وضعیت، و بک‌تست آزمایشی را انجام می‌دهد.\n"
    "هیچ معامله زنده‌ای از این مسیر اجرا نمی‌شود."
)

BACKTEST_TEXT = "📊 منوی بک‌تست آزمایشی"
SYSTEM_TEST_TEXT = "🧪 منوی تست‌های امن سیستم"
TOOBIT_STATUS_TEXT = "💰 وضعیت اتصال Toobit فقط به صورت حضور تنظیمات نمایش داده می‌شود."


def main_reply_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "📊 بک‌تست"}, {"text": "🧪 تست سیستم"}],
            [{"text": "📜 گزارش آخر"}, {"text": "Logs & Reports"}],
            [{"text": "💰 توبیت"}, {"text": "🌐 Dashboard"}],
            [{"text": "وضعیت"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def backtest_inline_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "اجرای بک‌تست fixture", "callback_data": "backtest:run"}],
            [{"text": "لغو", "callback_data": "backtest:cancel"}],
            [{"text": "بازگشت", "callback_data": "menu:main"}],
        ]
    }


def system_tests_inline_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "اجرای verify-system safe", "callback_data": "system:verify"}],
            [{"text": "نمایش آخرین گزارش", "callback_data": "system:last_report"}],
            [{"text": "بازگشت", "callback_data": "menu:main"}],
        ]
    }


def logs_inline_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Log Channel Status", "callback_data": "logs:status"}],
            [{"text": "Dry-run Test Log", "callback_data": "logs:test_dry"}],
            [{"text": "Last Processing Events", "callback_data": "logs:last_events"}],
            [{"text": "Back", "callback_data": "menu:main"}],
        ]
    }
