"""Pure Telegram update handlers for the admin bot runtime."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from triak_trade.admin_bot.auth import AdminAuthService, normalize_username
from triak_trade.admin_bot.menus import (
    BACKTEST_TEXT,
    SYSTEM_TEST_TEXT,
    TOOBIT_STATUS_TEXT,
    UNAUTHORIZED_TEXT,
    WELCOME_TEXT,
    backtest_inline_keyboard,
    logs_inline_keyboard,
    main_reply_keyboard,
    system_tests_inline_keyboard,
)
from triak_trade.admin_bot.state import AdminBotStateStore, utc_now
from triak_trade.backtesting.engine import run_fixture_backtest
from triak_trade.backtesting.real_runner import RealBacktestRunner, RealBacktestRunRequest
from triak_trade.config.settings import Settings
from triak_trade.observability.formatters import format_processing_audit_for_telegram
from triak_trade.observability.processing_audit import build_sample_processing_audit_event
from triak_trade.observability.telegram_log_channel import TelegramLogChannelClient
from triak_trade.verification.models import VerificationStatus
from triak_trade.verification.report import find_latest_report, render_terminal_summary
from triak_trade.verification.runner import VerificationRunner


class OutgoingMessage(BaseModel):
    chat_id: int
    text: str
    reply_markup: dict[str, Any] | None = None


class HandledUpdate(BaseModel):
    update_id: int | None = None
    username: str | None = None
    authorized: bool = False
    outgoing: list[OutgoingMessage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AdminBotUpdateHandler:
    """Handle Telegram Bot API update dictionaries without network access."""

    def __init__(
        self,
        *,
        settings: Settings,
        auth: AdminAuthService,
        state_store: AdminBotStateStore,
    ) -> None:
        self.settings = settings
        self.auth = auth
        self.state_store = state_store

    def handle_update(self, update: dict[str, Any]) -> HandledUpdate:
        update_id = _extract_update_id(update)
        chat_id = _extract_chat_id(update)
        username = _extract_username(update)
        text = _extract_text(update)
        callback_data = _extract_callback_data(update)
        authorized = self.auth.is_authorized_username(username)

        if chat_id is None:
            return HandledUpdate(
                update_id=update_id,
                username=normalize_username(username or "") or None,
                authorized=False,
                notes=["missing_chat_id"],
            )

        if not authorized:
            result = HandledUpdate(
                update_id=update_id,
                username=normalize_username(username or "") or None,
                authorized=False,
                outgoing=[OutgoingMessage(chat_id=chat_id, text=UNAUTHORIZED_TEXT)],
                notes=["unauthorized"],
            )
            self._record_handled(result)
            return result

        normalized_username = normalize_username(username or "")
        outgoing = self._route_authorized(chat_id, text, callback_data)
        result = HandledUpdate(
            update_id=update_id,
            username=normalized_username,
            authorized=True,
            outgoing=outgoing,
            notes=["authorized"],
        )
        self._record_handled(result)
        return result

    def _route_authorized(
        self,
        chat_id: int,
        text: str | None,
        callback_data: str | None,
    ) -> list[OutgoingMessage]:
        if callback_data is not None:
            return self._handle_callback(chat_id, callback_data)

        normalized_text = (text or "").strip()
        if normalized_text in {"/start", "start"}:
            return [
                OutgoingMessage(
                    chat_id=chat_id,
                    text=WELCOME_TEXT,
                    reply_markup=main_reply_keyboard(),
                )
            ]
        if normalized_text == "📊 بک‌تست":
            return [
                OutgoingMessage(
                    chat_id=chat_id,
                    text=BACKTEST_TEXT,
                    reply_markup=backtest_inline_keyboard(),
                )
            ]
        if normalized_text == "🧪 تست سیستم":
            return [
                OutgoingMessage(
                    chat_id=chat_id,
                    text=SYSTEM_TEST_TEXT,
                    reply_markup=system_tests_inline_keyboard(),
                )
            ]
        if normalized_text == "📜 گزارش آخر":
            return [OutgoingMessage(chat_id=chat_id, text=self._last_report_text())]
        if normalized_text == "Logs & Reports":
            return [
                OutgoingMessage(
                    chat_id=chat_id,
                    text="🛰️ Logs & Reports",
                    reply_markup=logs_inline_keyboard(),
                )
            ]
        if normalized_text == "💰 توبیت":
            return [OutgoingMessage(chat_id=chat_id, text=self._toobit_status_text())]
        if normalized_text == "🌐 Dashboard":
            return [OutgoingMessage(chat_id=chat_id, text=self._dashboard_text())]
        if normalized_text == "وضعیت":
            return [OutgoingMessage(chat_id=chat_id, text=self._runtime_status_text())]
        return [
            OutgoingMessage(
                chat_id=chat_id,
                text="دستور نامشخص است. لطفاً از منوی اصلی استفاده کنید.",
                reply_markup=main_reply_keyboard(),
            )
        ]

    def _handle_callback(self, chat_id: int, callback_data: str) -> list[OutgoingMessage]:
        if callback_data in {"menu:main", "backtest:cancel"}:
            return [
                OutgoingMessage(
                    chat_id=chat_id,
                    text=WELCOME_TEXT,
                    reply_markup=main_reply_keyboard(),
                )
            ]
        if callback_data == "backtest:run":
            _report_json, summary = run_fixture_backtest()
            text = (
                "📊 بک‌تست fixture کامل شد.\n"
                f"{summary}\n"
                "حالت: simulation only"
            )
            return [OutgoingMessage(chat_id=chat_id, text=text)]
        if callback_data in {"backtest:real:24h", "backtest:real:7d"}:
            hours = 24 if callback_data.endswith("24h") else 168
            return self._run_real_backtest(chat_id, hours=hours)
        if callback_data == "backtest:latest":
            return [OutgoingMessage(chat_id=chat_id, text=self._latest_backtest_text())]
        if callback_data == "backtest:dashboard":
            return [OutgoingMessage(chat_id=chat_id, text=self._dashboard_text())]
        if callback_data == "system:verify":
            report = VerificationRunner(self.settings).run(mode="safe", write_report=True)
            text = render_terminal_summary(report)
            if report.overall_status is VerificationStatus.FAIL:
                text = "بررسی امن سیستم با خطا تمام شد.\n" + text
            return [OutgoingMessage(chat_id=chat_id, text=_truncate(text))]
        if callback_data == "system:last_report":
            return [OutgoingMessage(chat_id=chat_id, text=self._last_report_text())]
        if callback_data == "logs:status":
            status = TelegramLogChannelClient(settings=self.settings).safe_status()
            text = (
                "Log Channel Status\n"
                f"channel={status['log_channel_username']}\n"
                f"enabled={status['enabled']}\n"
                f"audit_send_enabled={status['audit_send_enabled']}\n"
                f"guard_enabled={status['guard_enabled']}\n"
                f"bot_token_present={status['bot_token_present']}"
            )
            return [OutgoingMessage(chat_id=chat_id, text=text)]
        if callback_data == "logs:test_dry":
            event = build_sample_processing_audit_event(self.settings)
            return [
                OutgoingMessage(
                    chat_id=chat_id,
                    text=_truncate(format_processing_audit_for_telegram(event)),
                )
            ]
        if callback_data == "logs:last_events":
            return [
                OutgoingMessage(
                    chat_id=chat_id,
                    text="Last processing events require DB-backed listing in a later step.",
                )
            ]
        return [
            OutgoingMessage(
                chat_id=chat_id,
                text="این callback نیاز به تأیید دستی دارد و هیچ اجرایی انجام نشد.",
            )
        ]

    def _last_report_text(self) -> str:
        latest = find_latest_report(self.settings.VERIFICATION_REPORT_DIR)
        if latest is None:
            return "گزارشی هنوز ثبت نشده است."
        lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()
        preview = "\n".join(lines[:12])
        return _truncate(f"آخرین گزارش:\npath={latest}\n\n{preview}")

    def _latest_backtest_text(self) -> str:
        payload = RealBacktestRunner(settings=self.settings).latest_report_summary()
        if payload is None:
            return "هنوز هیچ real backtest report ذخیره نشده است."
        return _truncate(
            "📄 Latest real backtest report\n"
            f"path={payload.get('report_path')}\n"
            f"success={payload.get('success')}\n"
            f"real_telegram_used={payload.get('real_telegram_used')}\n"
            f"real_market_data_used={payload.get('real_market_data_used')}\n"
            f"parsed_signals={payload.get('parsed_signals')}\n"
            f"valid_signals={payload.get('valid_signals')}\n"
            f"trades_simulated={payload.get('trades_simulated')}\n"
            f"total_pnl={payload.get('total_pnl')}\n"
            f"channel_score={payload.get('channel_score')}"
        )

    def _run_real_backtest(self, chat_id: int, *, hours: int) -> list[OutgoingMessage]:
        runner = RealBacktestRunner(settings=self.settings)
        readiness = runner.readiness()
        if not readiness.ready:
            text = "Real backtest blocked.\n" + "\n".join(readiness.issues)
            return [OutgoingMessage(chat_id=chat_id, text=_truncate(text))]

        progress = [
            "🔎 Fetching Telegram messages...",
            "🧠 Classifying messages...",
            "🕯 Fetching Toobit candles...",
            "⚙️ Simulating trades...",
            "📊 Generating report...",
        ]
        request = RealBacktestRunRequest(
            channel=self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
            hours=hours,
            interval=self.settings.REAL_BACKTEST_DEFAULT_INTERVAL,
            max_messages=self.settings.REAL_BACKTEST_MAX_MESSAGES,
            use_ai=self.settings.REAL_BACKTEST_USE_AI,
            send_telegram_summary=self.settings.REAL_BACKTEST_SEND_TO_ADMIN_BOT,
            send_log_channel=self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL,
        )
        result = runner.run_sync(request)
        summary = (
            "✅ Backtest complete.\n"
            f"channel={result.channel}\n"
            f"real_telegram_used={result.real_telegram_used}\n"
            f"real_market_data_used={result.real_market_data_used}\n"
            f"ai_used={result.ai_used}\n"
            f"regex_fallback_used={result.regex_fallback_used}\n"
            f"total_messages={result.total_messages}\n"
            f"parsed_signals={result.parsed_signals}\n"
            f"valid_signals={result.valid_signals}\n"
            f"trades_simulated={result.trades_simulated}\n"
            f"total_pnl={result.total_pnl}\n"
            f"channel_score={result.channel_score}\n"
            f"dashboard=http://{self.settings.DASHBOARD_HOST}:{self.settings.DASHBOARD_PORT}/reports\n"
            f"report={result.report_path}"
        )
        if result.errors:
            summary += "\nerrors=" + "; ".join(result.errors)
        outgoing = [OutgoingMessage(chat_id=chat_id, text=item) for item in progress]
        outgoing.append(OutgoingMessage(chat_id=chat_id, text=_truncate(summary)))
        return outgoing

    def _toobit_status_text(self) -> str:
        key_present = _secret_present(self.settings.TOOBIT_API_KEY.get_secret_value())
        secret_present = _secret_present(self.settings.TOOBIT_API_SECRET.get_secret_value())
        return (
            f"{TOOBIT_STATUS_TEXT}\n"
            f"api_key_present={key_present}\n"
            f"api_secret_present={secret_present}\n"
            f"execution_mode={self.settings.EXECUTION_MODE}\n"
            "live_blocked=True"
        )

    def _runtime_status_text(self) -> str:
        state = self.state_store.read_status()
        return (
            "وضعیت runtime:\n"
            f"running={state.running}\n"
            f"pid={state.pid}\n"
            f"handled_updates_count={state.handled_updates_count}\n"
            f"last_update_id={state.last_update_id}"
        )

    def _dashboard_text(self) -> str:
        return (
            "Dashboard is local-only. Use your local machine/browser.\n"
            f"URL: http://{self.settings.DASHBOARD_HOST}:{self.settings.DASHBOARD_PORT}\n"
            "The admin token is stored in root .env.local and is not sent through Telegram."
        )

    def _record_handled(self, result: HandledUpdate) -> None:
        state = self.state_store.read_status()
        state.last_heartbeat_at = utc_now()
        state.handled_updates_count += 1
        state.last_update_id = result.update_id
        if result.username:
            state.last_admin_username = result.username
        self.state_store.write_status(state)


def _extract_update_id(update: dict[str, Any]) -> int | None:
    value = update.get("update_id")
    return value if isinstance(value, int) else None


def _extract_chat_id(update: dict[str, Any]) -> int | None:
    message = _message_or_callback_message(update)
    chat = message.get("chat") if isinstance(message, dict) else None
    if not isinstance(chat, dict):
        return None
    value = chat.get("id")
    return value if isinstance(value, int) else None


def _extract_username(update: dict[str, Any]) -> str | None:
    source = update.get("message")
    if "callback_query" in update and isinstance(update["callback_query"], dict):
        source = update["callback_query"]
    if not isinstance(source, dict):
        return None
    sender = source.get("from")
    if not isinstance(sender, dict):
        return None
    value = sender.get("username")
    return value if isinstance(value, str) else None


def _extract_text(update: dict[str, Any]) -> str | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    value = message.get("text")
    return value if isinstance(value, str) else None


def _extract_callback_data(update: dict[str, Any]) -> str | None:
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    value = callback.get("data")
    return value if isinstance(value, str) else None


def _message_or_callback_message(update: dict[str, Any]) -> dict[str, Any]:
    message = update.get("message")
    if isinstance(message, dict):
        return message
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        callback_message = callback.get("message")
        if isinstance(callback_message, dict):
            return callback_message
    return {}


def _secret_present(value: str) -> bool:
    return bool(value and value != "replace_me")


def _truncate(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n... truncated ..."
