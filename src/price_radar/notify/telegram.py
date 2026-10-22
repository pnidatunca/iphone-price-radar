"""Telegram notifications (plan 12.5): one message per health check, sent by the project's bot to its owner.

    python -m price_radar.notify.telegram chat-id       # after sending the bot a message: print the chat ids it saw
    python -m price_radar.notify.telegram send TEXT     # send a test message

The bot token and the chat id are secrets (``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``: GitHub Actions secrets, or
lines in the local .env). Without them nothing is sent and the caller carries on; a failed send is logged, never
raised, so a Telegram outage cannot fail the pipeline. Messages hold counts and dates only, no catalogue content.
"""

from __future__ import annotations

import argparse
import logging
import sys

import httpx

from price_radar.config import secret

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_LENGTH = 4096  # Telegram's limit for one message


def _quiet_httpx() -> None:
    # httpx logs every request URL at INFO level, and Telegram's URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configured() -> bool:
    return bool(secret("TELEGRAM_BOT_TOKEN") and secret("TELEGRAM_CHAT_ID"))


def send(text: str, *, transport: httpx.BaseTransport | None = None) -> bool:
    """Send ``text`` to the owner's chat. Returns False when Telegram is not configured or the send failed."""
    token, chat_id = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("Telegram is not configured; message not sent")
        return False
    if len(text) > MAX_LENGTH:
        text = text[: MAX_LENGTH - 1] + "…"
    _quiet_httpx()
    try:
        with httpx.Client(timeout=20, transport=transport) as client:
            response = client.post(
                f"{API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
    except httpx.HTTPError as err:
        log.warning("Telegram send failed: %s", type(err).__name__)  # the message may carry the token: not logged
        return False
    if response.status_code != 200:
        log.warning("Telegram send failed: HTTP %s", response.status_code)
        return False
    return True


def chat_ids(*, transport: httpx.BaseTransport | None = None) -> list[tuple[int, str]]:
    """Chats that have written to the bot recently: (chat id, name). Telegram keeps these updates for 24 hours."""
    token = secret("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (environment or .env)")
    _quiet_httpx()
    with httpx.Client(timeout=20, transport=transport) as client:
        response = client.get(f"{API}/bot{token}/getUpdates")
    response.raise_for_status()
    chats: dict[int, str] = {}
    for update in response.json().get("result", []):
        chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat") or {}
        if isinstance(chat.get("id"), int):
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    return sorted(chats.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.notify.telegram")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("chat-id", help="print the chat ids that wrote to the bot (send it a message first)")
    send_cmd = commands.add_parser("send", help="send a message")
    send_cmd.add_argument("text")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "chat-id":
        try:
            found = chat_ids()
        except (RuntimeError, httpx.HTTPError) as err:
            print(
                f"cannot read the bot's updates: {type(err).__name__}: {err if isinstance(err, RuntimeError) else ''}"
            )
            return 1
        if not found:
            print("no chat has written to the bot in the last 24 hours: send it a message, then run this again")
        for chat_id, name in found:
            print(f"{chat_id}\t{name}")
        return 0
    return 0 if send(args.text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
