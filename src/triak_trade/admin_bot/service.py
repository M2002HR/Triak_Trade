"""Admin approval workflow service."""

from __future__ import annotations

from typing import Any

from triak_trade.admin_bot.auth import AdminAuthService, normalize_username
from triak_trade.admin_bot.callbacks import parse_admin_callback
from triak_trade.admin_bot.errors import AdminRegistrationError
from triak_trade.admin_bot.telegram_bot import TelegramAdminBot
from triak_trade.backtesting.engine import run_fixture_backtest
from triak_trade.backtesting.real_runner import RealBacktestRunner, RealBacktestRunRequest
from triak_trade.config.settings import Settings
from triak_trade.db.repositories import AdminDecisionRepository
from triak_trade.domain.models import AdminDecision, ProposedAction, SignalState


class AdminApprovalService:
    def __init__(
        self,
        *,
        auth: AdminAuthService,
        bot: TelegramAdminBot,
        decisions: AdminDecisionRepository | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.auth = auth
        self.bot = bot
        self.decisions = decisions
        self.settings = settings

    async def send_for_approval(
        self,
        action: ProposedAction,
        signal: SignalState | None = None,
    ) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        for username in sorted(self.auth.allowed_usernames):
            reg = self.bot.registrations.get(username)
            if reg is None:
                raise AdminRegistrationError("Admin must start the bot first.")
            response = await self.bot.send_proposed_action(reg.chat_id, action, signal)
            message_id = None
            result = response.get("result")
            if isinstance(result, dict):
                message_id = result.get("message_id")
            sent.append({"username": username, "message_id": message_id})
        return sent

    def handle_callback(self, username: str | None, callback_data: str) -> AdminDecision:
        self.auth.require_authorized_username(username)
        parsed = parse_admin_callback(callback_data)
        decision = self.bot.handle_callback(normalize_username(username or ""), callback_data)
        decision.action_id = parsed.action_id
        decision.decision = parsed.decision
        if self.decisions is not None:
            self.decisions.save_decision(decision)
        return decision

    def backtest_menu(self, username: str | None) -> dict[str, object]:
        self.auth.require_authorized_username(username)
        return {
            "text": "📊 Backtest Menu",
            "buttons": [
                "🌪 Tofan_Trade real 24h",
                "🌪 Tofan_Trade real 7d",
                "⚙️ Custom backtest in dashboard",
                "📄 Latest backtest report",
                "🧪 Fixture backtest",
                "❌ Cancel",
                "⬅️ Main Menu",
            ],
            "callbacks": [
                "menu:backtest",
                "backtest:real:24h",
                "backtest:real:7d",
                "backtest:dashboard",
                "backtest:latest",
                "backtest:run",
                "backtest:cancel",
            ],
        }

    def run_backtest_dry(self, username: str | None) -> dict[str, object]:
        self.auth.require_authorized_username(username)
        report_json, summary = run_fixture_backtest()
        return {
            "progress": [
                "🔎 Fetching messages...",
                "🧠 Classifying signals...",
                "🕯 Fetching candles...",
                "⚙️ Simulating trades...",
                "📊 Calculating metrics...",
                "✅ Backtest complete.",
            ],
            "summary": summary,
            "report": report_json,
        }

    def run_real_backtest(self, username: str | None, *, hours: int) -> dict[str, object]:
        self.auth.require_authorized_username(username)
        if self.settings is None:
            raise AdminRegistrationError("settings are required for real backtest")
        runner = RealBacktestRunner(settings=self.settings)
        readiness = runner.readiness()
        if not readiness.ready:
            return {
                "blocked": True,
                "issues": readiness.issues,
                "progress": [],
            }
        request = RealBacktestRunRequest(
            channel=self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
            hours=hours,
            interval=self.settings.REAL_BACKTEST_DEFAULT_INTERVAL,
            max_messages=self.settings.REAL_BACKTEST_MAX_MESSAGES,
            use_ai=self.settings.REAL_BACKTEST_USE_AI,
            send_telegram_summary=self.settings.REAL_BACKTEST_SEND_TO_ADMIN_BOT,
            send_log_channel=self.settings.REAL_BACKTEST_SEND_TO_LOG_CHANNEL,
            log_per_message=self.settings.REAL_BACKTEST_LOG_PER_MESSAGE,
        )
        result = runner.run_sync(request)
        return {
            "blocked": False,
            "progress": [
                "🔎 Fetching Telegram messages...",
                "🧠 Classifying messages...",
                "🕯 Fetching Toobit candles...",
                "⚙️ Simulating trades...",
                "📊 Generating report...",
                "✅ Backtest complete.",
            ],
            "summary": {
                "real_telegram_used": result.real_telegram_used,
                "real_market_data_used": result.real_market_data_used,
                "ai_used": result.ai_used,
                "regex_fallback_used": result.regex_fallback_used,
                "total_messages": result.total_messages,
                "parsed_signals": result.parsed_signals,
                "valid_signals": result.valid_signals,
                "trades_simulated": result.trades_simulated,
                "total_pnl": str(result.total_pnl),
                "channel_score": str(result.channel_score),
                "errors": result.errors,
                "warnings": result.warnings,
                "report_path": result.report_path,
            },
        }

    def latest_backtest_report(self, username: str | None) -> dict[str, object]:
        self.auth.require_authorized_username(username)
        if self.settings is None:
            raise AdminRegistrationError("settings are required for latest real backtest report")
        payload = RealBacktestRunner(settings=self.settings).latest_report_summary()
        return {"report": payload}
