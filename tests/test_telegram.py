import json

import httpx
import pytest

from price_radar.notify import telegram


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


def test_without_secrets_nothing_is_sent(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(telegram, "secret", lambda name: None)  # and no .env fallback
    sent = []
    transport = httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200))
    assert telegram.send("hello", transport=transport) is False
    assert sent == []


def test_a_message_goes_to_the_owners_chat(configured):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    assert telegram.send("x" * 5000, transport=httpx.MockTransport(handler)) is True
    (request,) = sent
    assert request.url == "https://api.telegram.org/bot123:secret/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == "42" and len(body["text"]) == telegram.MAX_LENGTH


def test_a_failed_send_does_not_raise(configured):
    assert telegram.send("x", transport=httpx.MockTransport(lambda r: httpx.Response(401))) is False

    def broken(request):
        raise httpx.ConnectError("down")

    assert telegram.send("x", transport=httpx.MockTransport(broken)) is False


def test_chat_ids_come_from_the_bots_updates(configured):
    updates = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"chat": {"id": 42, "first_name": "Owner", "type": "private"}}},
            {"update_id": 2, "message": {"chat": {"id": 42, "first_name": "Owner", "type": "private"}}},
        ],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=updates))
    assert telegram.chat_ids(transport=transport) == [(42, "Owner")]
