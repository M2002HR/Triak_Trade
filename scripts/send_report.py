"""Send a user-requested report to Telegram via the existing Telethon user session.

Per CLAUDE.md / AGENTS.md, user-requested reports go to `we_are_waiting_for_him`
through the Telethon user session configured in root `.env.local` — never through
the bot API or the `@triak_logs` processing-audit channel.

Usage:
    python scripts/send_report.py <recipient> --file <path-to-utf8-text-file>
    python scripts/send_report.py <recipient> --text "inline message"
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from triak_trade.config.settings import get_settings
from triak_trade.telegram.telethon_client import TelethonTelegramClient


async def send(recipient: str, text: str) -> None:
    """Send `text` to `recipient` using the project's configured Telethon client."""
    # Reuse the project client so proxy handling, docker overrides, and credential
    # validation stay identical to the rest of the system.
    client = TelethonTelegramClient(get_settings())._build_client()
    async with client:
        await client.send_message(recipient, text)
    print(f"sent to {recipient} ({len(text)} chars)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipient", help="Telegram username, id, or t.me link")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Path to a UTF-8 text file holding the message body")
    source.add_argument("--text", help="Inline message body")
    args = parser.parse_args()

    text = (
        Path(args.file).read_text(encoding="utf-8") if args.file else str(args.text)
    ).strip()
    if not text:
        raise SystemExit("message body is empty")
    asyncio.run(send(args.recipient, text))


if __name__ == "__main__":
    main()
