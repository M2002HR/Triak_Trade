from __future__ import annotations

from datetime import datetime, timezone

from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.agents.clock import FakeClock
from triak_trade.config.settings import Settings
from triak_trade.domain.enums import ProposedActionType, SignalStatus
from triak_trade.domain.models import RawTelegramMessage


def _agent(
    *,
    channel_id: str = "chan-a",
    max_update_hours: int = 48,
    context_limit: int = 50,
) -> tuple[ChannelAgent, FakeClock, Settings]:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    settings = Settings(
        SIGNAL_CONSOLIDATION_SECONDS=180,
        SIGNAL_MAX_UPDATE_WINDOW_HOURS=max_update_hours,
        CHANNEL_AGENT_CONTEXT_MESSAGE_LIMIT=context_limit,
    )
    clock = FakeClock(now)
    return ChannelAgent(channel_id=channel_id, settings=settings, clock=clock), clock, settings


def _raw(
    *,
    clock: FakeClock,
    channel_id: str,
    message_id: int,
    text: str,
    reply_to: int | None = None,
) -> RawTelegramMessage:
    return RawTelegramMessage(
        channel_id=channel_id,
        channel_username="u",
        message_id=message_id,
        text=text,
        date=clock.now(),
        edited_at=None,
        reply_to_msg_id=reply_to,
    )


def test_new_valid_signal_pending_no_immediate_create_order() -> None:
    agent, clock, _ = _agent()
    actions = agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
        )
    )
    assert actions == []
    snap = agent.get_context_snapshot()
    signal_state = next(iter(snap["signals"].values()))
    assert signal_state["status"] == SignalStatus.PENDING_CONSOLIDATION.value


def test_tick_before_and_after_consolidation_window() -> None:
    agent, clock, settings = _agent()
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x",
        )
    )
    clock.advance(seconds=100)
    assert agent.tick(clock.now()) == []

    clock.advance(seconds=settings.SIGNAL_CONSOLIDATION_SECONDS - 100)
    actions = agent.tick(clock.now())
    assert len(actions) == 1
    assert actions[0].action_type is ProposedActionType.CREATE_ORDER


def test_related_messages_attached_in_consolidation() -> None:
    agent, clock, settings = _agent()
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000",
        )
    )
    clock.advance(seconds=30)
    agent.ingest_message(
        _raw(clock=clock, channel_id="chan-a", message_id=2, text="SL: 67400", reply_to=1)
    )
    clock.advance(seconds=30)
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=3,
            text="TP: 69000 / 70000",
            reply_to=1,
        )
    )
    clock.advance(seconds=30)
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=4,
            text="Leverage: 5x",
            reply_to=1,
        )
    )

    snap = agent.get_context_snapshot()
    signal_state = next(iter(snap["signals"].values()))
    assert set(signal_state["related_message_ids"]) == {1, 2, 3, 4}

    clock.advance(seconds=settings.SIGNAL_CONSOLIDATION_SECONDS)
    actions = agent.tick(clock.now())
    assert actions[0].action_type is ProposedActionType.CREATE_ORDER


def test_split_signal_across_three_consecutive_messages_merges_without_reply() -> None:
    agent, clock, settings = _agent()
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 - 68200",
        )
    )
    clock.advance(seconds=15)
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=2,
            text="TP: 69000 / 70000",
        )
    )
    clock.advance(seconds=15)
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=3,
            text="SL: 67400",
        )
    )

    snap = agent.get_context_snapshot()
    signal_state = next(iter(snap["signals"].values()))
    assert set(signal_state["related_message_ids"]) == {1, 2, 3}

    clock.advance(seconds=settings.SIGNAL_CONSOLIDATION_SECONDS)
    actions = agent.tick(clock.now())
    assert actions[0].action_type is ProposedActionType.CREATE_ORDER


def test_irrelevant_ad_and_profit_report_ignored() -> None:
    agent, clock, _ = _agent()
    ad_actions = agent.ingest_message(
        _raw(clock=clock, channel_id="chan-a", message_id=1, text="Join VIP now! giveaway")
    )
    profit_actions = agent.ingest_message(
        _raw(clock=clock, channel_id="chan-a", message_id=2, text="TP1 hit ✅ +120% profit")
    )
    assert ad_actions == []
    assert profit_actions == []


def test_followup_mapping_after_proposal() -> None:
    agent, clock, settings = _agent()
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000",
        )
    )
    clock.advance(seconds=settings.SIGNAL_CONSOLIDATION_SECONDS)
    agent.tick(clock.now())

    cancel_actions = agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=2,
            text="cancel BTCUSDT signal",
            reply_to=1,
        )
    )
    assert cancel_actions[0].action_type is ProposedActionType.CANCEL_PENDING_ORDER

    lev_actions = agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=3,
            text="update leverage to 3x",
            reply_to=1,
        )
    )
    assert lev_actions[0].action_type is ProposedActionType.UPDATE_LEVERAGE

    sl_actions = agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=4,
            text="move SL to breakeven",
            reply_to=1,
        )
    )
    assert sl_actions[0].action_type is ProposedActionType.MOVE_STOP_LOSS

    tp_actions = agent.ingest_message(
        _raw(clock=clock, channel_id="chan-a", message_id=5, text="TP updated to 70500", reply_to=1)
    )
    assert tp_actions[0].action_type is ProposedActionType.UPDATE_TAKE_PROFIT


def test_ambiguous_update_is_ignored_safely() -> None:
    agent, clock, settings = _agent()
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000",
        )
    )
    clock.advance(seconds=settings.SIGNAL_CONSOLIDATION_SECONDS)
    agent.tick(clock.now())

    actions = agent.ingest_message(
        _raw(clock=clock, channel_id="chan-a", message_id=2, text="Entry updated", reply_to=1)
    )
    assert actions[0].action_type is ProposedActionType.IGNORE_MESSAGE


def test_multiple_channels_independent_and_multi_pending() -> None:
    agent_a, clock_a, _ = _agent(channel_id="chan-a")
    agent_b, clock_b, _ = _agent(channel_id="chan-b")

    agent_a.ingest_message(
        _raw(
            clock=clock_a,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 SL: 67400 TP: 69000",
        )
    )
    agent_b.ingest_message(
        _raw(
            clock=clock_b,
            channel_id="chan-b",
            message_id=1,
            text="ETHUSDT SHORT Entry: 3800 SL: 3900 TP: 3600",
        )
    )

    assert len(agent_a.get_context_snapshot()["signals"]) == 1
    assert len(agent_b.get_context_snapshot()["signals"]) == 1

    agent_a.ingest_message(
        _raw(
            clock=clock_a,
            channel_id="chan-a",
            message_id=2,
            text="ETHUSDT LONG Entry: 3800 SL: 3700 TP: 3900",
        )
    )
    assert len(agent_a.get_context_snapshot()["signals"]) == 2


def test_context_limit_and_update_window_enforced() -> None:
    agent, clock, _settings = _agent(max_update_hours=1, context_limit=3)
    for idx in range(1, 6):
        agent.ingest_message(
            _raw(clock=clock, channel_id="chan-a", message_id=idx, text=f"msg {idx}")
        )
    assert agent.get_context_snapshot()["recent_message_ids"] == [3, 4, 5]

    agent2, clock2, settings2 = _agent(max_update_hours=1)
    agent2.ingest_message(
        _raw(
            clock=clock2,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 SL: 67400 TP: 69000",
        )
    )
    clock2.advance(seconds=settings2.SIGNAL_CONSOLIDATION_SECONDS)
    agent2.tick(clock2.now())
    clock2.advance(hours=2)
    actions = agent2.ingest_message(
        _raw(clock=clock2, channel_id="chan-a", message_id=2, text="cancel BTCUSDT signal")
    )
    assert actions == []


def test_snapshot_readable_non_secret() -> None:
    agent, clock, _ = _agent()
    agent.ingest_message(
        _raw(
            clock=clock,
            channel_id="chan-a",
            message_id=1,
            text="BTCUSDT LONG Entry: 68000 SL: 67400 TP: 69000",
        )
    )
    snap = agent.get_context_snapshot()
    assert "signals" in snap
    assert "debug_events" in snap
    assert "TOOBIT_API_KEY" not in str(snap)
