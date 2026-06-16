"""Verification checks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from triak_trade import __version__
from triak_trade.admin_bot.auth import AdminAuthService
from triak_trade.admin_bot.formatter import AdminActionFormatter
from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.agents.clock import FakeClock
from triak_trade.ai.classifier import AIMessageClassifier
from triak_trade.ai.gateway_client import AjilGatewayClient
from triak_trade.backtesting.engine import run_fixture_backtest
from triak_trade.cache.redis_client import build_redis_from_settings
from triak_trade.config.settings import Settings
from triak_trade.db.engine import build_engine_from_settings
from triak_trade.domain.enums import ProposedActionType
from triak_trade.domain.models import ProposedAction, RawTelegramMessage
from triak_trade.exchange.toobit.account import ToobitAccountClient
from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.market_data.binance_public import BinancePublicFuturesProvider
from triak_trade.market_data.toobit import ToobitMarketDataProvider
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser
from triak_trade.telegram.client import FakeTelegramClient
from triak_trade.telegram.telethon_client import TelethonTelegramClient
from triak_trade.verification.models import VerificationCheckResult, VerificationStatus

CheckCallable = Callable[[Settings], VerificationCheckResult]


def safe_checks() -> list[CheckCallable]:
    return [
        config_check,
        python_package_check,
        db_engine_check,
        redis_client_factory_check,
        parser_check,
        channel_agent_check,
        ai_dry_run_check,
        telegram_dry_run_check,
        market_data_fake_check,
        toobit_safety_check,
        admin_bot_dry_run_check,
        backtest_fixture_check,
    ]


def real_checks() -> list[CheckCallable]:
    return [
        mysql_real_check,
        redis_real_check,
        telegram_real_tofan_check,
        binance_public_real_check,
        toobit_public_real_check,
        toobit_signed_real_check,
        toobit_ordertest_real_check,
        ai_gateway_real_check,
        telegram_bot_real_check,
        backtest_real_guarded_check,
    ]


def _result(
    *,
    name: str,
    status: VerificationStatus,
    category: str,
    summary: str,
    started: float,
    details: dict[str, object] | None = None,
    error_type: str | None = None,
    next_action: str | None = None,
) -> VerificationCheckResult:
    return VerificationCheckResult(
        name=name,
        status=status,
        category=category,
        summary=summary,
        details=details or {},
        duration_ms=int((time.perf_counter() - started) * 1000),
        error_type=error_type,
        next_action=next_action,
    )


def config_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    return _result(
        name="config",
        status=VerificationStatus.PASS,
        category="safe",
        summary="settings loaded",
        started=started,
        details={"app_env": settings.APP_ENV, "execution_mode": settings.EXECUTION_MODE},
    )


def python_package_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    return _result(
        name="python_package",
        status=VerificationStatus.PASS,
        category="safe",
        summary="package import/version ok",
        started=started,
        details={"version": __version__},
    )


def db_engine_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    engine = build_engine_from_settings(settings)
    return _result(
        name="db_engine",
        status=VerificationStatus.PASS,
        category="safe",
        summary="SQLAlchemy engine created without connecting",
        started=started,
        details={"dialect": engine.dialect.name},
    )


def redis_client_factory_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    client = build_redis_from_settings(settings)
    return _result(
        name="redis_client_factory",
        status=VerificationStatus.PASS,
        category="safe",
        summary="Redis client factory created without ping",
        started=started,
        details={"client_type": client.__class__.__name__},
    )


def parser_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    normalizer = MessageNormalizer()
    parser = RegexSignalParser()
    samples = {
        "valid": "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000",
        "profit": "TP1 hit +120% profit",
        "cancel": "cancel BTC signal",
        "ambiguous": "BTC looking good",
    }
    actions: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    for index, (key, text) in enumerate(samples.items(), start=1):
        raw = RawTelegramMessage(
            channel_id="verify",
            channel_username=None,
            message_id=index,
            text=text,
            date=now,
            edited_at=None,
            reply_to_msg_id=None,
        )
        actions[key] = parser.parse(normalizer.normalize(raw)).action.value
    ok = actions["valid"] == "open" and actions["profit"] == "ignore"
    ok = ok and actions["cancel"] == "cancel" and actions["ambiguous"] in {"unknown", "ignore"}
    return _result(
        name="parser",
        status=VerificationStatus.PASS if ok else VerificationStatus.FAIL,
        category="safe",
        summary="deterministic parser samples classified",
        started=started,
        details={"actions": actions},
    )


def channel_agent_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    clock = FakeClock(datetime.now(timezone.utc))
    agent = ChannelAgent(channel_id="verify-agent", settings=settings, clock=clock)
    msg = RawTelegramMessage(
        channel_id="verify-agent",
        channel_username="verify",
        message_id=1,
        text="BTCUSDT LONG Entry: 100 - 101 SL: 98 TP: 104",
        date=clock.now(),
        edited_at=None,
        reply_to_msg_id=None,
    )
    immediate = agent.ingest_message(msg)
    clock.advance(seconds=settings.SIGNAL_CONSOLIDATION_SECONDS)
    after_tick = agent.tick(clock.now())
    ok = not immediate and any(action.action_type.value == "create_order" for action in after_tick)
    return _result(
        name="channel_agent",
        status=VerificationStatus.PASS if ok else VerificationStatus.FAIL,
        category="safe",
        summary="consolidation waits, then proposes action",
        started=started,
        details={"immediate_actions": len(immediate), "tick_actions": len(after_tick)},
    )


def ai_dry_run_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()

    def handler(request: httpx.Request) -> httpx.Response:
        request_json = json.loads(request.content.decode("utf-8"))
        text = ""
        messages = request_json.get("messages")
        if isinstance(messages, list) and len(messages) > 1 and isinstance(messages[1], dict):
            content = messages[1].get("content")
            if isinstance(content, str):
                try:
                    content_payload = json.loads(content)
                except ValueError:
                    text = content.lower()
                else:
                    context_payload = content_payload.get("context", {})
                    if isinstance(context_payload, dict):
                        message_text = context_payload.get("message_text", "")
                        text = str(message_text).lower()
        body = {
            "classification": "NEW_SIGNAL",
            "action": "open",
            "market": "futures",
            "symbol": "BTCUSDT",
            "side": "long",
            "entry_type": "market",
            "entry_low": None,
            "entry_high": None,
            "stop_loss": "98",
            "take_profits": ["104"],
            "leverage": 2,
            "related_signal_id": None,
            "relation_reason": None,
            "confidence": "0.85",
            "reasoning_summary": "mock",
            "risk_notes": [],
            "requires_admin_confirmation": True,
            "raw_provider_metadata": {"mode": "mock"},
        }
        if "profit" in text:
            body["classification"] = "RESULT_REPORT"
            body["action"] = "ignore"
        if "giveaway" in text:
            body["classification"] = "ADVERTISEMENT"
            body["action"] = "ignore"
        return httpx.Response(200, json=body)

    client = AjilGatewayClient(
        base_url="http://mock.local",
        timeout_seconds=1,
        classify_path=settings.AI_GATEWAY_CLASSIFY_PATH,
        retry_attempts=settings.AI_GATEWAY_RETRY_ATTEMPTS,
        retry_backoff_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    classifier = AIMessageClassifier(settings=settings, gateway_client=client)
    from triak_trade.agents.context import ChannelContext

    context = ChannelContext(channel_id="ai", max_message_limit=10, max_update_window_hours=1)
    now = datetime.now(timezone.utc)
    samples = ["BTCUSDT LONG SL 98 TP 104", "TP1 hit profit", "promo giveaway"]
    actions: list[str] = []
    httpx_logger = logging.getLogger("httpx")
    previous_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)
    try:
        for index, text in enumerate(samples, start=1):
            raw = RawTelegramMessage(
                channel_id="ai",
                channel_username=None,
                message_id=index,
                text=text,
                date=now,
                edited_at=None,
                reply_to_msg_id=None,
            )
            actions.append(classifier.classify(raw, context).parsed_signal.action.value)
    finally:
        httpx_logger.setLevel(previous_level)
    ok = actions == ["open", "ignore", "ignore"]
    return _result(
        name="ai_dry_run",
        status=VerificationStatus.PASS if ok else VerificationStatus.FAIL,
        category="safe",
        summary="AI classifier mock path works",
        started=started,
        details={"actions": actions},
    )


def telegram_dry_run_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    now = datetime.now(timezone.utc)
    messages = [
        RawTelegramMessage(
            channel_id=settings.TELEGRAM_REAL_TEST_CHANNEL,
            channel_username="tofan_trade",
            message_id=1,
            text="fixture",
            date=now,
            edited_at=None,
            reply_to_msg_id=None,
        )
    ]
    client = FakeTelegramClient(history_by_channel={settings.TELEGRAM_REAL_TEST_CHANNEL: messages})
    fetched = asyncio.run(client.fetch_history(settings.TELEGRAM_REAL_TEST_CHANNEL, limit=1))
    return _result(
        name="telegram_dry_run",
        status=VerificationStatus.PASS if len(fetched) == 1 else VerificationStatus.FAIL,
        category="safe",
        summary="fake Telegram history fetch works",
        started=started,
        details={"message_count": len(fetched), "message_ids": [m.message_id for m in fetched]},
    )


def market_data_fake_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    from triak_trade.backtesting.fixtures import fixture_candles

    candles = fixture_candles(interval=settings.TOOBIT_MARKET_DATA_DEFAULT_INTERVAL)
    return _result(
        name="market_data_fake",
        status=VerificationStatus.PASS if candles else VerificationStatus.FAIL,
        category="safe",
        summary="fixture candles available",
        started=started,
        details={"candle_count": len(candles), "symbol": candles[0].symbol if candles else None},
    )


def toobit_safety_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    source_files = list(Path("src/triak_trade/exchange/toobit").rglob("*.py"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    has_withdraw = "withdraw" in combined.lower()
    live_blocked = str(settings.EXECUTION_MODE) != "live"
    ok = live_blocked and not has_withdraw
    return _result(
        name="toobit_safety",
        status=VerificationStatus.PASS if ok else VerificationStatus.FAIL,
        category="safe",
        summary="live mode blocked and withdrawal endpoints absent",
        started=started,
        details={"live_blocked": live_blocked, "withdrawal_reference_found": has_withdraw},
    )


def admin_bot_dry_run_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    auth = AdminAuthService(settings.ADMIN_TELEGRAM_USERNAMES)
    formatter = AdminActionFormatter()
    action = ProposedAction(
        action_id="verify_action",
        action_type=ProposedActionType.CREATE_ORDER,
        signal_id="sig",
        risk_increasing=True,
        requires_admin_approval=True,
        confidence=Decimal("0.8"),
        reason="verification",
        payload={"symbol": "BTCUSDT"},
        created_at=datetime.now(timezone.utc),
    )
    formatted = formatter.format_action(action)
    authorized = auth.is_authorized_username(settings.ADMIN_TELEGRAM_USERNAMES[0])
    unauthorized = auth.is_authorized_username("@not_allowed")
    ok = authorized and not unauthorized and "Demo only" in formatted.text
    return _result(
        name="admin_bot_dry_run",
        status=VerificationStatus.PASS if ok else VerificationStatus.FAIL,
        category="safe",
        summary="admin auth and formatter work",
        started=started,
        details={"buttons": len(formatted.buttons), "authorized_default_admin": authorized},
    )


def backtest_fixture_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    report_json, summary = run_fixture_backtest()
    ok = "metrics" in report_json and "Backtest Report" in summary
    return _result(
        name="backtest_fixture",
        status=VerificationStatus.PASS if ok else VerificationStatus.FAIL,
        category="safe",
        summary="fixture backtest report generated",
        started=started,
        details={
            "final_balance": report_json.get("final_balance"),
            "channel_score": report_json.get("channel_score"),
        },
    )


def mysql_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if settings.RUN_MYSQL_INTEGRATION_TESTS != 1:
        return _skip("mysql_real", started, "set RUN_MYSQL_INTEGRATION_TESTS=1")
    try:
        engine = build_engine_from_settings(settings)
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        status = VerificationStatus.PASS
        summary = "MySQL connectivity ok"
        error_type = None
    except Exception as exc:
        status = VerificationStatus.FAIL
        summary = "MySQL connectivity failed"
        error_type = type(exc).__name__
    return _result(
        name="mysql_real",
        status=status,
        category="real",
        summary=summary,
        started=started,
        error_type=error_type,
    )


def redis_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if settings.RUN_REDIS_INTEGRATION_TESTS != 1:
        return _skip("redis_real", started, "set RUN_REDIS_INTEGRATION_TESTS=1")
    try:
        client = build_redis_from_settings(settings)
        client.ping()
        status = VerificationStatus.PASS
        summary = "Redis ping ok"
        error_type = None
    except Exception as exc:
        status = VerificationStatus.FAIL
        summary = "Redis ping failed"
        error_type = type(exc).__name__
    return _result(
        name="redis_real",
        status=status,
        category="real",
        summary=summary,
        started=started,
        error_type=error_type,
    )


def telegram_real_tofan_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1 or settings.RUN_TELEGRAM_INTEGRATION_TESTS != 1:
        return _skip("telegram_real_tofan", started, "enable system and Telegram guards")
    try:
        client = TelethonTelegramClient(settings)
        messages = asyncio.run(
            client.fetch_history(
                settings.TELEGRAM_REAL_TEST_CHANNEL,
                limit=settings.VERIFICATION_MAX_REAL_TELEGRAM_MESSAGES,
            )
        )
        dates = [message.date.isoformat() for message in messages]
        return _result(
            name="telegram_real_tofan",
            status=VerificationStatus.PASS,
            category="real",
            summary="Telegram small fetch completed",
            started=started,
            details={
                "message_count": len(messages),
                "first_date": dates[0] if dates else None,
                "last_date": dates[-1] if dates else None,
                "message_ids": [message.message_id for message in messages],
            },
        )
    except Exception as exc:
        return _result(
            name="telegram_real_tofan",
            status=VerificationStatus.FAIL,
            category="real",
            summary="Telegram small fetch failed",
            started=started,
            error_type=type(exc).__name__,
        )


def toobit_public_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if (
        settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1
        or settings.RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS != 1
    ):
        return _skip("toobit_public_real", started, "enable system and Toobit market-data guards")
    try:
        provider = ToobitMarketDataProvider(
            base_url=settings.TOOBIT_BASE_URL,
            klines_path=settings.TOOBIT_KLINES_PATH,
            mark_price_klines_path=settings.TOOBIT_FUTURES_MARK_PRICE_KLINES_PATH,
            index_klines_path=settings.TOOBIT_FUTURES_INDEX_KLINES_PATH,
            contract_ticker_price_path=settings.TOOBIT_FUTURES_TICKER_PRICE_PATH,
            timeout_seconds=settings.TOOBIT_MARKET_DATA_TIMEOUT_SECONDS,
            limit=10,
        )
        end = datetime.now(timezone.utc)
        candles = asyncio.run(
            provider.get_klines(
                settings.TOOBIT_REAL_TEST_SYMBOL,
                "1m",
                end - timedelta(minutes=5),
                end,
            )
        )
        return _result(
            name="toobit_public_real",
            status=VerificationStatus.PASS,
            category="real",
            summary="Toobit public kline fetch completed",
            started=started,
            details={"candle_count": len(candles)},
        )
    except Exception as exc:
        return _result(
            name="toobit_public_real",
            status=VerificationStatus.FAIL,
            category="real",
            summary="Toobit public kline fetch failed",
            started=started,
            error_type=type(exc).__name__,
        )


def binance_public_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if (
        settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1
        or settings.RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS != 1
    ):
        return _skip(
            "binance_public_real",
            started,
            "enable system and Binance public market-data guards",
        )
    try:
        provider = BinancePublicFuturesProvider(
            base_url=settings.BINANCE_PUBLIC_DATA_BASE_URL,
            rest_base_url=settings.BINANCE_FUTURES_REST_BASE_URL,
            klines_path=settings.BINANCE_FUTURES_KLINES_PATH,
            ticker_price_path=settings.BINANCE_FUTURES_TICKER_PRICE_PATH,
            cache_dir=settings.BINANCE_PUBLIC_DATA_CACHE_DIR,
            timeout_seconds=settings.BINANCE_PUBLIC_DATA_TIMEOUT_SECONDS,
        )
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        candles = asyncio.run(
            provider.get_klines(
                settings.BINANCE_PUBLIC_REAL_TEST_SYMBOL,
                "1m",
                end - timedelta(minutes=5),
                end,
            )
        )
        return _result(
            name="binance_public_real",
            status=VerificationStatus.PASS,
            category="real",
            summary="Binance public historical futures fetch completed",
            started=started,
            details={"candle_count": len(candles)},
        )
    except Exception as exc:
        return _result(
            name="binance_public_real",
            status=VerificationStatus.FAIL,
            category="real",
            summary="Binance public historical futures fetch failed",
            started=started,
            error_type=type(exc).__name__,
        )


def toobit_signed_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if (
        settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1
        or settings.RUN_TOOBIT_SIGNED_INTEGRATION_TESTS != 1
    ):
        return _skip("toobit_signed_real", started, "enable system and Toobit signed guards")
    if not settings.TOOBIT_SAFE_ACCOUNT_PATH:
        return _skip("toobit_signed_real", started, "configure TOOBIT_SAFE_ACCOUNT_PATH")
    client = _toobit_client(settings)
    account = ToobitAccountClient(client, settings.TOOBIT_SAFE_ACCOUNT_PATH)
    try:
        result = asyncio.run(account.safe_account_check())
        return _result(
            name="toobit_signed_real",
            status=VerificationStatus.PASS,
            category="real",
            summary="safe signed account check completed",
            started=started,
            details=result.model_dump(mode="json"),
        )
    except Exception as exc:
        return _result(
            name="toobit_signed_real",
            status=VerificationStatus.FAIL,
            category="real",
            summary="safe signed account check failed",
            started=started,
            error_type=type(exc).__name__,
        )


def toobit_ordertest_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if (
        settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1
        or settings.RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS != 1
    ):
        return _skip("toobit_ordertest_real", started, "enable system and orderTest guards")
    if settings.EXECUTION_MODE != "demo":
        return _skip("toobit_ordertest_real", started, "set EXECUTION_MODE=demo")
    if not settings.TOOBIT_ORDERTEST_QUANTITY or not settings.TOOBIT_ORDERTEST_PRICE:
        return _skip("toobit_ordertest_real", started, "configure orderTest quantity and price")
    return _result(
        name="toobit_ordertest_real",
        status=VerificationStatus.SKIP,
        category="real",
        summary="orderTest real path is CLI-guarded; run explicit command when needed",
        started=started,
        next_action="run triak-trade toobit-order-test --real with explicit params",
    )


def ai_gateway_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if (
        settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1
        or settings.RUN_AI_GATEWAY_INTEGRATION_TESTS != 1
        or not settings.AI_GATEWAY_ENABLED
    ):
        return _skip("ai_gateway_real", started, "enable system, AI gateway guard, and gateway")
    return _result(
        name="ai_gateway_real",
        status=VerificationStatus.SKIP,
        category="real",
        summary="real AI gateway check is available through ai-classify-dry-run --real-gateway",
        started=started,
        next_action="run guarded AI gateway CLI with safe sample messages",
    )


def telegram_bot_real_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if (
        settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1
        or settings.RUN_TELEGRAM_BOT_INTEGRATION_TESTS != 1
    ):
        return _skip("telegram_bot_real", started, "enable system and Telegram bot guards")
    return _result(
        name="telegram_bot_real",
        status=VerificationStatus.SKIP,
        category="real",
        summary="no registered admin chat_id available in verification context",
        started=started,
        next_action="Admin must start the bot or provide chat_id.",
    )


def backtest_real_guarded_check(settings: Settings) -> VerificationCheckResult:
    started = time.perf_counter()
    if settings.RUN_SYSTEM_REAL_SMOKE_TESTS != 1 or settings.RUN_BACKTEST_INTEGRATION_TESTS != 1:
        return _skip("backtest_real_guarded", started, "enable system and backtest guards")
    if (
        settings.RUN_TELEGRAM_INTEGRATION_TESTS != 1
        or settings.RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS != 1
    ):
        return _skip(
            "backtest_real_guarded",
            started,
            "enable Telegram and Toobit market-data guards",
        )
    return _result(
        name="backtest_real_guarded",
        status=VerificationStatus.SKIP,
        category="real",
        summary="real backtest orchestration is guarded for a later tiny-window run",
        started=started,
        next_action=(
            "run triak-trade real-backtest-run --channel https://t.me/Tofan_Trade "
            "--hours 1 --max-messages 5 --interval 1m "
            "--no-send-telegram-summary --no-send-log-channel"
        ),
    )


def _skip(name: str, started: float, next_action: str) -> VerificationCheckResult:
    return _result(
        name=name,
        status=VerificationStatus.SKIP,
        category="real",
        summary="guard disabled",
        started=started,
        next_action=next_action,
    )


def _toobit_client(settings: Settings) -> ToobitClient:
    return ToobitClient(
        base_url=settings.TOOBIT_BASE_URL,
        api_key=settings.TOOBIT_API_KEY.get_secret_value(),
        api_secret=settings.TOOBIT_API_SECRET.get_secret_value(),
        timeout_seconds=settings.TOOBIT_SIGNED_TIMEOUT_SECONDS,
        recv_window=settings.TOOBIT_RECV_WINDOW,
        time_path=settings.TOOBIT_TIME_PATH,
        exchange_info_path=settings.TOOBIT_EXCHANGE_INFO_PATH,
    )
