"""CLI entrypoint."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import typer

from triak_trade import __version__
from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.agents.clock import FakeClock
from triak_trade.agents.context import ChannelContext
from triak_trade.ai.classifier import AIMessageClassifier
from triak_trade.ai.gateway_client import AjilGatewayClient
from triak_trade.config.settings import Settings, get_settings
from triak_trade.core.health import run_health_checks
from triak_trade.core.logging import configure_logging
from triak_trade.db.engine import build_engine_from_settings
from triak_trade.domain.enums import CandleSource
from triak_trade.domain.models import Candle, RawTelegramMessage
from triak_trade.exchange.toobit.account import ToobitAccountClient
from triak_trade.exchange.toobit.client import ToobitClient
from triak_trade.exchange.toobit.demo_execution import DemoExecutionAdapter
from triak_trade.exchange.toobit.errors import ToobitError
from triak_trade.exchange.toobit.spot import ToobitSpotClient
from triak_trade.market_data.toobit import ToobitMarketDataProvider
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser
from triak_trade.parsing.validator import ParsedSignalValidator
from triak_trade.telegram.client import FakeTelegramClient, TelegramClientInterface
from triak_trade.telegram.telethon_client import TelethonTelegramClient

app = typer.Typer(no_args_is_help=True)


def _load_settings() -> Settings:
    settings = get_settings()
    configure_logging(settings)
    return settings


def _build_toobit_client(settings: Settings) -> ToobitClient:
    return ToobitClient(
        base_url=settings.TOOBIT_BASE_URL,
        api_key=settings.TOOBIT_API_KEY.get_secret_value(),
        api_secret=settings.TOOBIT_API_SECRET.get_secret_value(),
        timeout_seconds=settings.TOOBIT_SIGNED_TIMEOUT_SECONDS,
        recv_window=settings.TOOBIT_RECV_WINDOW,
        time_path=settings.TOOBIT_TIME_PATH,
        exchange_info_path=settings.TOOBIT_EXCHANGE_INFO_PATH,
    )


@app.command("version")
def version_cmd() -> None:
    """Show app version."""
    typer.echo(__version__)


@app.command("health")
def health_cmd(
    include_services: bool = typer.Option(False, help="Include DB/Redis checks."),
) -> None:
    """Run health checks."""
    settings = _load_settings()
    result = run_health_checks(settings=settings, include_services=include_services)
    typer.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@app.command("config-check")
def config_check_cmd() -> None:
    """Validate config and print safe status."""
    _load_settings()
    typer.echo("Configuration is valid")


@app.command("db-check")
def db_check_cmd() -> None:
    """Build DB engine from config without connecting."""
    settings = _load_settings()
    engine = build_engine_from_settings(settings)
    typer.echo(f"DB engine configured (dialect={engine.dialect.name})")


@app.command("parse-message")
def parse_message_cmd(message: str) -> None:
    """Normalize, parse, and validate a single message safely."""
    settings = _load_settings()

    raw = RawTelegramMessage(
        channel_id="cli",
        channel_username=None,
        message_id=1,
        text=message,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )
    normalizer = MessageNormalizer()
    parser = RegexSignalParser()
    validator = ParsedSignalValidator()

    normalized = normalizer.normalize(raw)
    parsed = parser.parse(normalized)
    ok, errors = validator.validate_for_proposal(
        parsed,
        max_leverage=settings.MAX_LEVERAGE,
        require_stop_loss=settings.REQUIRE_STOP_LOSS,
    )

    payload = {
        "normalized_text": normalized.normalized_text,
        "detected_symbols": normalized.detected_symbols,
        "detected_keywords": normalized.detected_keywords,
        "parsed": parsed.model_dump(mode="json"),
        "proposal_valid": ok,
        "validation_errors": errors,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("agent-dry-run")
def agent_dry_run_cmd() -> None:
    """Run a deterministic in-memory channel agent simulation."""
    settings = _load_settings()
    start = datetime.now(timezone.utc)
    clock = FakeClock(start)
    agent = ChannelAgent(channel_id="dry-run-channel", settings=settings, clock=clock)

    sequence = [
        (0, "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x"),
        (30, "SL: 67400"),
        (60, "TP: 69000 / 70000"),
        (90, "Join VIP now!"),
    ]

    immediate_actions: list[dict[str, object]] = []
    for idx, (offset_sec, text) in enumerate(sequence, start=1):
        clock.advance(seconds=offset_sec - (0 if idx == 1 else sequence[idx - 2][0]))
        message = RawTelegramMessage(
            channel_id="dry-run-channel",
            channel_username="dry",
            message_id=idx,
            text=text,
            date=clock.now(),
            edited_at=None,
            reply_to_msg_id=1 if idx in {2, 3} else None,
        )
        for action in agent.ingest_message(message):
            immediate_actions.append(action.model_dump(mode="json"))

    clock.advance(seconds=max(0, settings.SIGNAL_CONSOLIDATION_SECONDS - 90))
    tick_actions = [action.model_dump(mode="json") for action in agent.tick(clock.now())]

    snapshot = agent.get_context_snapshot()
    payload = {
        "pending_signal_ids": snapshot.get("pending_signal_ids", []),
        "signal_statuses": snapshot.get("signals", {}),
        "immediate_actions": immediate_actions,
        "tick_actions": tick_actions,
        "counts": {
            "immediate_actions": len(immediate_actions),
            "tick_actions": len(tick_actions),
        },
        "safety": {"requires_admin_approval_default": True, "no_execution": True},
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("ai-classify-dry-run")
def ai_classify_dry_run_cmd(message: str, real_gateway: bool = typer.Option(False)) -> None:
    """Run AI classifier in safe dry-run mode."""
    settings = _load_settings()
    now = datetime.now(timezone.utc)
    raw = RawTelegramMessage(
        channel_id="ai-dry-run",
        channel_username="dry",
        message_id=1,
        text=message,
        date=now,
        edited_at=None,
        reply_to_msg_id=None,
    )
    context = ChannelContext(
        channel_id="ai-dry-run",
        max_message_limit=settings.CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT,
        max_update_window_hours=settings.SIGNAL_MAX_UPDATE_WINDOW_HOURS,
    )
    context.add_recent_message(raw)

    using_real_gateway = (
        real_gateway
        and settings.AI_GATEWAY_ENABLED
        and os.getenv("RUN_AI_GATEWAY_INTEGRATION_TESTS") == "1"
    )

    if using_real_gateway:
        client = AjilGatewayClient(
            base_url=settings.AI_GATEWAY_BASE_URL,
            timeout_seconds=settings.AI_GATEWAY_TIMEOUT_SECONDS,
            classify_path=settings.AI_GATEWAY_CLASSIFY_PATH,
        )
        mode = "real-gateway"
    else:
        def handler(request: httpx.Request) -> httpx.Response:
            body: dict[str, object] = {
                "classification": "UNKNOWN",
                "action": "unknown",
                "market": "unknown",
                "symbol": "BTCUSDT" if "btc" in message.lower() else None,
                "side": "long" if "long" in message.lower() else "unknown",
                "entry_type": "unknown",
                "entry_low": None,
                "entry_high": None,
                "stop_loss": None,
                "take_profits": [],
                "leverage": None,
                "related_signal_id": None,
                "relation_reason": None,
                "confidence": "0.35",
                "reasoning_summary": "dry-run mock response",
                "risk_notes": ["no real gateway call"],
                "requires_admin_confirmation": True,
                "raw_provider_metadata": {"mode": "mock"},
            }
            if "entry" in message.lower() and "sl" in message.lower() and "tp" in message.lower():
                body.update(
                    {
                        "classification": "NEW_SIGNAL",
                        "action": "open",
                        "market": "futures",
                        "entry_type": "range",
                        "entry_low": "68000",
                        "entry_high": "68200",
                        "stop_loss": "67400",
                        "take_profits": ["69000", "70000"],
                        "leverage": 5,
                        "confidence": "0.88",
                        "reasoning_summary": "structured signal detected",
                    }
                )
            elif "profit" in message.lower() or "tp1 hit" in message.lower():
                body.update(
                    {
                        "classification": "RESULT_REPORT",
                        "action": "ignore",
                        "confidence": "0.85",
                        "reasoning_summary": "result report not a new signal",
                    }
                )
            elif (
                "promo" in message.lower()
                or "giveaway" in message.lower()
                or "join now" in message.lower()
            ):
                body.update(
                    {
                        "classification": "ADVERTISEMENT",
                        "action": "ignore",
                        "confidence": "0.86",
                        "reasoning_summary": "promotional content",
                    }
                )
            return httpx.Response(200, json=body)

        client = AjilGatewayClient(
            base_url="http://mocked.local",
            timeout_seconds=settings.AI_GATEWAY_TIMEOUT_SECONDS,
            classify_path=settings.AI_GATEWAY_CLASSIFY_PATH,
            transport=httpx.MockTransport(handler),
        )
        mode = "mock-gateway"

    classifier = AIMessageClassifier(settings=settings, gateway_client=client)
    classified = classifier.classify(raw, context)
    payload: dict[str, object] = {
        "mode": mode,
        "real_gateway_requested": real_gateway,
        "real_gateway_used": using_real_gateway,
        "parsed_action": classified.parsed_signal.action.value,
        "parsed_symbol": classified.parsed_signal.symbol,
        "classification_confidence": str(classified.confidence),
        "is_potential_new_signal": classified.is_potential_new_signal,
        "is_related_to_existing_signal": classified.is_related_to_existing_signal,
        "debug_notes": classified.debug_notes,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("telegram-check")
def telegram_check_cmd() -> None:
    """Validate Telegram config without connecting."""
    settings = _load_settings()
    api_hash = settings.TELEGRAM_API_HASH.get_secret_value()
    payload = {
        "api_id_present": settings.TELEGRAM_API_ID > 0,
        "api_hash_present": bool(api_hash and api_hash != "replace_me"),
        "session_dir": settings.TELEGRAM_SESSION_DIR,
        "session_name": settings.TELEGRAM_SESSION_NAME,
        "real_test_channel": settings.TELEGRAM_REAL_TEST_CHANNEL,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("telegram-history-dry-run")
def telegram_history_dry_run_cmd(
    channel: str,
    limit: int = typer.Option(5, min=1),
    real: bool = typer.Option(False, "--real"),
) -> None:
    """Fetch Telegram history in fake mode by default."""
    settings = _load_settings()
    use_real = real and settings.RUN_TELEGRAM_INTEGRATION_TESTS == 1
    if real and not use_real:
        raise typer.BadParameter(
            "Real Telegram mode requires RUN_TELEGRAM_INTEGRATION_TESTS=1 in root .env.local"
        )

    if use_real:
        client: TelegramClientInterface = TelethonTelegramClient(settings)
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "real"
    else:
        now = datetime.now(timezone.utc)
        fake_messages = [
            RawTelegramMessage(
                channel_id=channel,
                channel_username="fake_channel",
                message_id=i + 1,
                text=f"sample message {i + 1}",
                date=now,
                edited_at=None,
                reply_to_msg_id=None,
            )
            for i in range(limit)
        ]
        client = FakeTelegramClient(history_by_channel={channel: fake_messages})
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "fake"

    payload = {
        "mode": mode,
        "channel": channel,
        "count": len(messages),
        "sample": [
            {
                "channel_id": item.channel_id,
                "message_id": item.message_id,
                "date": item.date.isoformat(),
            }
            for item in messages[:5]
        ],
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("telegram-tofan-dry-run")
def telegram_tofan_dry_run_cmd(
    limit: int = typer.Option(20, min=1),
    real: bool = typer.Option(False, "--real"),
    show_text: bool = typer.Option(False, "--show-text"),
) -> None:
    """Run dry-run fetch for configured real test channel."""
    settings = _load_settings()
    channel = settings.TELEGRAM_REAL_TEST_CHANNEL
    use_real = real and settings.RUN_TELEGRAM_INTEGRATION_TESTS == 1
    if real and not use_real:
        raise typer.BadParameter(
            "Real Telegram mode requires RUN_TELEGRAM_INTEGRATION_TESTS=1 in root .env.local"
        )

    if use_real:
        client: TelegramClientInterface = TelethonTelegramClient(settings)
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "real"
    else:
        now = datetime.now(timezone.utc)
        fake_messages = [
            RawTelegramMessage(
                channel_id=channel,
                channel_username="tofan_trade",
                message_id=i + 100,
                text=f"fixture text {i + 1}",
                date=now,
                edited_at=None,
                reply_to_msg_id=None,
            )
            for i in range(limit)
        ]
        client = FakeTelegramClient(history_by_channel={channel: fake_messages})
        messages = asyncio.run(client.fetch_history(channel, limit=limit))
        mode = "fake"

    sorted_messages = sorted(messages, key=lambda item: item.date)
    payload: dict[str, object] = {
        "mode": mode,
        "channel": channel,
        "count": len(messages),
        "text_message_count": sum(1 for item in messages if item.text),
        "sample_message_ids": [item.message_id for item in messages[:5]],
    }
    if sorted_messages:
        payload["first_date"] = sorted_messages[0].date.isoformat()
        payload["last_date"] = sorted_messages[-1].date.isoformat()
    if show_text:
        payload["sample_text"] = [item.text for item in messages[:3]]
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("market-data-dry-run")
def market_data_dry_run_cmd(
    symbol: str,
    interval: str = typer.Option("1m"),
    minutes: int = typer.Option(5, min=1),
) -> None:
    """Generate fake candle output without network calls."""
    _load_settings()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candles: list[Candle] = []
    base = Decimal("100")
    for idx in range(minutes):
        open_time = now - timedelta(minutes=minutes - idx)
        open_price = base + Decimal(str(idx))
        close_price = open_price + Decimal("0.2")
        candles.append(
            Candle(
                symbol=symbol.upper(),
                interval=interval,
                open_time=open_time,
                close_time=open_time + timedelta(minutes=1),
                open=open_price,
                high=close_price + Decimal("0.1"),
                low=open_price - Decimal("0.1"),
                close=close_price,
                volume=Decimal("10"),
                source=CandleSource.FIXTURE,
            )
        )
    payload = {
        "symbol": symbol.upper(),
        "interval": interval,
        "candle_count": len(candles),
        "first_open_time": candles[0].open_time.isoformat() if candles else None,
        "last_open_time": candles[-1].open_time.isoformat() if candles else None,
        "source": CandleSource.FIXTURE.value,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-klines-dry-run")
def toobit_klines_dry_run_cmd(
    symbol: str,
    interval: str = typer.Option("1m"),
    minutes: int = typer.Option(5, min=1),
    real: bool = typer.Option(False, "--real"),
) -> None:
    """Run guarded Toobit public kline fetch."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real with integration guard enabled.")
    if settings.RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "Real mode requires RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=1 in root .env.local"
        )

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=minutes)
    provider = ToobitMarketDataProvider(
        base_url=settings.TOOBIT_BASE_URL,
        klines_path=settings.TOOBIT_KLINES_PATH,
        timeout_seconds=settings.TOOBIT_MARKET_DATA_TIMEOUT_SECONDS,
        limit=settings.TOOBIT_MARKET_DATA_LIMIT,
    )
    candles = asyncio.run(provider.get_klines(symbol, interval, start_time, end_time))
    payload = {
        "symbol": symbol.upper(),
        "interval": interval,
        "candle_count": len(candles),
        "first_open_time": candles[0].open_time.isoformat() if candles else None,
        "last_open_time": candles[-1].open_time.isoformat() if candles else None,
        "source": CandleSource.TOOBIT.value if candles else "none",
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-check")
def toobit_check_cmd() -> None:
    """Show safe Toobit configuration presence summary."""
    settings = _load_settings()
    payload = {
        "base_url": settings.TOOBIT_BASE_URL,
        "api_key_present": bool(
            settings.TOOBIT_API_KEY.get_secret_value()
            and settings.TOOBIT_API_KEY.get_secret_value() != "replace_me"
        ),
        "api_secret_present": bool(
            settings.TOOBIT_API_SECRET.get_secret_value()
            and settings.TOOBIT_API_SECRET.get_secret_value() != "replace_me"
        ),
        "execution_mode": settings.EXECUTION_MODE,
        "live_blocked": True,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-public-check")
def toobit_public_check_cmd() -> None:
    """Call public Toobit endpoints and print safe summary."""
    settings = _load_settings()
    client = _build_toobit_client(settings)
    try:
        time_payload = asyncio.run(client.get_server_time())
        exchange_payload = asyncio.run(client.get_exchange_info())
        payload = {
            "server_time_keys": sorted(list(time_payload.keys())),
            "exchange_info_keys": sorted(list(exchange_payload.keys())),
            "public_check_success": True,
        }
    except Exception as exc:
        payload = {
            "public_check_success": False,
            "error_type": type(exc).__name__,
            "detail": "public endpoint check failed safely",
        }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("toobit-signed-check")
def toobit_signed_check_cmd(real: bool = typer.Option(False, "--real")) -> None:
    """Run guarded signed safe account check."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real and enable guard.")
    if settings.RUN_TOOBIT_SIGNED_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "Real signed mode requires RUN_TOOBIT_SIGNED_INTEGRATION_TESTS=1 in root .env.local"
        )

    client = _build_toobit_client(settings)
    account = ToobitAccountClient(client, settings.TOOBIT_SAFE_ACCOUNT_PATH)
    try:
        result = asyncio.run(account.safe_account_check())
    except ToobitError as exc:
        raise typer.BadParameter(f"signed check failed safely: {type(exc).__name__}") from exc
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


@app.command("toobit-order-test")
def toobit_order_test_cmd(
    symbol: str = typer.Option(..., "--symbol"),
    side: str = typer.Option(..., "--side"),
    order_type: str = typer.Option(..., "--type"),
    quantity: str = typer.Option(..., "--quantity"),
    price: str | None = typer.Option(None, "--price"),
    real: bool = typer.Option(False, "--real"),
) -> None:
    """Run guarded spot orderTest only (no live order)."""
    settings = _load_settings()
    if not real:
        raise typer.BadParameter("Blocked by default. Pass --real and enable guard.")
    if settings.RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS != 1:
        raise typer.BadParameter(
            "OrderTest real mode requires RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS=1 in .env.local"
        )
    if settings.EXECUTION_MODE != "demo":
        raise typer.BadParameter("OrderTest requires EXECUTION_MODE=demo")
    try:
        quantity_decimal = Decimal(quantity)
        price_decimal = Decimal(price) if price is not None else None
    except Exception as exc:
        raise typer.BadParameter("quantity/price must be valid decimals") from exc

    client = _build_toobit_client(settings)
    spot = ToobitSpotClient(client, settings)
    adapter = DemoExecutionAdapter(settings, spot)
    try:
        result = asyncio.run(
            adapter.create_demo_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity_decimal,
                price=price_decimal,
                run_order_test=True,
            )
        )
    except Exception as exc:
        raise typer.BadParameter(f"order-test failed safely: {type(exc).__name__}") from exc

    payload = {
        "accepted": result.accepted,
        "symbol": result.symbol,
        "side": result.side,
        "type": result.order_type,
        "status": result.status,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
