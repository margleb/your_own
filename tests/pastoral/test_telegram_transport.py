"""Exercise the shared Telegram wire over HTTP with private error handling."""
from __future__ import annotations

import logging

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from infrastructure.telegram.client import TelegramClient, TelegramError

MARKER = "СЕКРЕТНЫЙ_ТЕКСТ_НЕ_ДЛЯ_ЖУРНАЛОВ"
TOKEN = "private-test-token"


@pytest_asyncio.fixture
async def telegram_wire(monkeypatch):
    state = {"requests": [], "body": {"ok": True, "result": True}, "raw": None}

    async def respond(request):
        state["requests"].append((request.match_info["method"], await request.json()))
        if state["raw"] is not None:
            return web.Response(text=state["raw"], content_type="text/plain")
        return web.json_response(state["body"])

    app = web.Application()
    app.router.add_post("/bot" + TOKEN + "/{method}", respond)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr("infrastructure.telegram.client._API", f"http://127.0.0.1:{port}")
    yield state
    await runner.cleanup()


@pytest.fixture
def private_logs(caplog):
    logger = logging.getLogger("telegram")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="telegram")
    yield caplog
    logger.removeHandler(caplog.handler)


@pytest.mark.asyncio
async def test_shared_session_is_reused_and_callback_updates_are_requested(telegram_wire, monkeypatch):
    async with aiohttp.ClientSession() as session:
        client = TelegramClient(TOKEN, session=session, private=True)

        def forbidden_session(*args, **kwargs):
            raise AssertionError("The injected Telegram session must be reused")

        monkeypatch.setattr("infrastructure.telegram.client.aiohttp.ClientSession", forbidden_session)
        telegram_wire["body"] = {"ok": True, "result": {"is_bot": True, "id": 111}}
        assert (await client.get_me())["is_bot"] is True
        telegram_wire["body"] = {"ok": True, "result": [{"update_id": 8, "callback_query": {"id": "query-8"}}]}
        updates = await client.get_updates(8, timeout=0, allowed_updates=["message", "callback_query"])
        assert updates[0]["callback_query"]["id"] == "query-8"
        telegram_wire["body"] = {"ok": True, "result": True}
        await client.answer_callback_query("query-8", "Готово")
        assert session.closed is False
        assert telegram_wire["requests"] == [
            ("getMe", {}),
            ("getUpdates", {"offset": 8, "timeout": 0, "allowed_updates": ["message", "callback_query"]}),
            ("answerCallbackQuery", {"callback_query_id": "query-8", "text": "Готово"}),
        ]


@pytest.mark.asyncio
async def test_rate_limit_keeps_numeric_retry_metadata_without_body(telegram_wire, private_logs):
    telegram_wire["body"] = {"ok": False, "error_code": 429, "description": MARKER, "parameters": {"retry_after": 7}}
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramError) as caught:
            await TelegramClient(TOKEN, session=session, private=True).send_message(1, MARKER)
    assert caught.value.status == 429
    assert caught.value.retry_after == 7
    assert caught.value.description == "api_error"
    assert MARKER not in str(caught.value)
    assert MARKER not in private_logs.text
    assert len(telegram_wire["requests"]) == 1  # sender, not transport, owns retries


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"ok": False, "error_code": MARKER, "description": MARKER, "parameters": {"retry_after": MARKER}}, 0),
    ({"ok": False, "error_code": None, "description": MARKER, "parameters": [MARKER]}, 0),
    ({"ok": False, "error_code": 429, "description": MARKER, "parameters": {"retry_after": float("inf")}}, 429),
    ({"ok": False, "error_code": 429, "description": MARKER, "parameters": {"retry_after": -1}}, 429),
    ([MARKER], 0),
])
async def test_malformed_error_values_cannot_escape_as_private_exception_text(telegram_wire, private_logs, body, status):
    telegram_wire["body"] = body
    async with aiohttp.ClientSession() as session:
        with pytest.raises(TelegramError) as caught:
            await TelegramClient(TOKEN, session=session, private=True).get_me()
    assert caught.value.status == status
    assert caught.value.retry_after is None
    assert MARKER not in str(caught.value)
    assert MARKER not in private_logs.text


@pytest.mark.asyncio
async def test_non_json_response_and_network_exception_are_content_free(telegram_wire, private_logs, monkeypatch):
    telegram_wire["raw"] = MARKER
    async with aiohttp.ClientSession() as session:
        client = TelegramClient(TOKEN, session=session, private=True)
        with pytest.raises(TelegramError, match="network_or_decode") as caught:
            await client.get_me()
        assert MARKER not in str(caught.value)

        def disconnected(*args, **kwargs):
            raise aiohttp.ClientError(MARKER + " " + TOKEN)

        monkeypatch.setattr(session, "post", disconnected)
        with pytest.raises(TelegramError, match="network_or_decode") as caught:
            await client.send_message(1, MARKER)
        assert MARKER not in str(caught.value)
        assert TOKEN not in str(caught.value)
    assert MARKER not in private_logs.text
    assert TOKEN not in private_logs.text


@pytest.mark.asyncio
async def test_oversized_message_is_rejected_and_fitting_text_is_never_truncated(telegram_wire):
    async with aiohttp.ClientSession() as session:
        client = TelegramClient(TOKEN, session=session, private=True)
        with pytest.raises(ValueError, match="split it before sending"):
            await client.send_message(1, "я" * 4097)
        assert telegram_wire["requests"] == []
        text = "я" * 4096
        telegram_wire["body"] = {"ok": True, "result": {"message_id": 1}}
        await client.send_message(1, text)
        assert telegram_wire["requests"] == [("sendMessage", {"chat_id": 1, "text": text})]


@pytest.mark.asyncio
async def test_default_miniapp_button_is_configured_through_shared_transport(telegram_wire):
    url = "https://example.org/pastoral/"
    menu = {"type": "web_app", "text": "Открыть приложение", "web_app": {"url": url}}
    async with aiohttp.ClientSession() as session:
        client = TelegramClient(TOKEN, session=session, private=True)
        assert await client.set_chat_menu_button(menu["text"], url)
        telegram_wire["body"] = {"ok": True, "result": menu}
        assert await client.get_chat_menu_button() == menu
    assert telegram_wire["requests"] == [("setChatMenuButton", {"menu_button": menu}), ("getChatMenuButton", {})]
