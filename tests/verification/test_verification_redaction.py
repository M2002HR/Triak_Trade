from __future__ import annotations

from triak_trade.verification.redaction import redact, redact_text


def test_redaction_removes_fake_secrets() -> None:
    payload = {
        "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyzABCDE",
        "TOOBIT_API_KEY": "fake-key",
        "TOOBIT_API_SECRET": "fake-secret",
        "nested": {"signature": "abcdef1234567890abcdef1234567890"},
        "safe": "visible",
    }
    redacted = redact(payload)
    assert redacted["TELEGRAM_BOT_TOKEN"] == "***REDACTED***"
    assert redacted["TOOBIT_API_KEY"] == "***REDACTED***"
    assert redacted["TOOBIT_API_SECRET"] == "***REDACTED***"
    assert redacted["nested"]["signature"] == "***REDACTED***"
    assert redacted["safe"] == "visible"
    assert redact({"telegram_bot_token_present": True})["telegram_bot_token_present"] is True


def test_redaction_removes_sensitive_patterns() -> None:
    text = "signature=abcdef1234567890abcdef1234567890 X-BB-APIKEY: abc"
    assert "abcdef123456" not in redact_text(text)
    assert "X-BB-APIKEY: abc" not in redact_text(text)
