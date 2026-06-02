from __future__ import annotations

from triak_trade.exchange.toobit.signer import ToobitSigner


def test_signer_deterministic_and_order_stable_and_lowercase() -> None:
    signer = ToobitSigner("secret")
    a = signer.sign({"b": 2, "a": 1})
    b = signer.sign({"a": 1, "b": 2})
    assert a == b
    assert a == a.lower()


def test_signer_no_mutation_and_no_secret_in_repr() -> None:
    signer = ToobitSigner("super-secret")
    params = {"z": 3}
    _ = signer.sign(params)
    assert params == {"z": 3}
    assert "super-secret" not in repr(signer)
