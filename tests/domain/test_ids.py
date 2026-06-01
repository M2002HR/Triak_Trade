from __future__ import annotations

from triak_trade.domain.ids import make_action_id, make_client_order_id, make_signal_id


def test_make_signal_id_is_deterministic() -> None:
    assert make_signal_id("channel-1", 10) == make_signal_id("channel-1", 10)


def test_make_signal_id_changes_with_input() -> None:
    assert make_signal_id("channel-1", 10) != make_signal_id("channel-2", 10)


def test_make_action_id_is_deterministic() -> None:
    value = make_action_id("sig_abc", "create_order", 1)
    assert value == make_action_id("sig_abc", "create_order", 1)


def test_make_client_order_id_is_deterministic() -> None:
    value = make_client_order_id("demo", "act_abc")
    assert value == make_client_order_id("demo", "act_abc")
    assert value != make_client_order_id("demo", "act_xyz")
