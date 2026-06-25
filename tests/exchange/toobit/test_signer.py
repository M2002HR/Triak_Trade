from __future__ import annotations

from triak_trade.exchange.toobit.signer import ToobitSigner


def test_signer_deterministic_and_insertion_order_and_lowercase() -> None:
    # Toobit signs in insertion order (not alphabetical) — same params same order = same sig
    signer = ToobitSigner("secret")
    a = signer.sign({"a": 1, "b": 2})
    b = signer.sign({"a": 1, "b": 2})
    assert a == b            # deterministic
    assert a == a.lower()    # lowercase hex

    # Different insertion order → different signature (insertion-order sensitive)
    c = signer.sign({"b": 2, "a": 1})
    assert a != c


def test_signer_no_mutation_and_no_secret_in_repr() -> None:
    signer = ToobitSigner("super-secret")
    params = {"z": 3}
    _ = signer.sign(params)
    assert params == {"z": 3}
    assert "super-secret" not in repr(signer)
