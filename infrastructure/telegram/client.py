"""Telegram Bot API client — the wire, and only the wire.

Long polling rather than a webhook: the backend runs on a laptop as often as
on the server, and a webhook needs a public address the laptop does not have.
``getUpdates`` with a timeout is one open request at a time, which is what a
bot in one group needs.

The token is read from settings at call time, like Pushy's, so a change on the
settings page is in force on the next poll without a restart.
"""
from __future__ import annotations

import math
from typing import Any

import aiohttp

from infrastructure.logging.logger import setup_logger

# setup_logger, not logging.getLogger: a bare logger has no handler and sits
# under the root's WARNING level, so every INFO line here — a poll, a trigger,
# a choice to stay silent — was written for nobody. Found the first time his
# silence had to be explained from the journal and the journal had nothing.
logger = setup_logger("telegram")

_API = "https://api.telegram.org"

# Telegram caps a message at 4096 characters; over that the API refuses it.
MESSAGE_MAX_CHARS = 4096
# ...and a photo caption at 1024.
CAPTION_MAX_CHARS = 1024


class TelegramError(RuntimeError):
    def __init__(self, status: int, description: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(f"telegram {status}: {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after

    @property
    def conflict(self) -> bool:
        """Another process is polling with this token.

        Telegram allows one ``getUpdates`` consumer per bot; a second one gets
        409 until the first stops. Worth naming, because the symptom otherwise
        is a bot that stays silent while every log line says it is polling.
        """
        return self.status == 409


class TelegramClient:
    def __init__(self, token: str, *, session: aiohttp.ClientSession | None = None, private: bool = False) -> None:
        self.token = token
        self.session = session
        self.private = private

    def _url(self, method: str) -> str:
        return f"{_API}/bot{self.token}/{method}"

    async def _call(self, method: str, params: dict | None = None, *, http_timeout: float) -> Any:
        payload = {k: v for k, v in (params or {}).items() if v is not None}
        async def request(session):
            async with session.post(
                self._url(method), json=payload,
                timeout=aiohttp.ClientTimeout(total=http_timeout),
            ) as resp:
                return await resp.json(content_type=None)
        try:
            if self.session is not None:
                body = await request(self.session)
            else:
                async with aiohttp.ClientSession() as session:
                    body = await request(session)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            if self.private:
                raise TelegramError(0, "network_or_decode") from None
            raise TelegramError(0, f"network: {exc}") from exc

        if not isinstance(body, dict) or not body.get("ok"):
            if isinstance(body, dict):
                parameters = body.get("parameters")
                parameters = parameters if isinstance(parameters, dict) else {}
                try:
                    status = int(body.get("error_code", 0))
                except (ValueError, TypeError):
                    status = 0
                try:
                    retry_after = float(parameters.get("retry_after"))
                    if not math.isfinite(retry_after) or retry_after < 0:
                        retry_after = None
                except (ValueError, TypeError):
                    retry_after = None
                raise TelegramError(
                    status,
                    "api_error" if self.private else str(body.get("description", "")),
                    retry_after=retry_after,
                )
            raise TelegramError(0, "invalid_response" if self.private else str(body))
        return body.get("result")

    async def get_me(self) -> dict:
        """Who the bot is: id and username. Cached by the listener."""
        return await self._call("getMe", http_timeout=15)

    async def set_chat_menu_button(self, text: str, url: str) -> bool:
        """Set the bot's default Mini App entry point for private chats."""
        return bool(await self._call(
            "setChatMenuButton",
            {"menu_button": {"type": "web_app", "text": text, "web_app": {"url": url}}},
            http_timeout=15,
        ))

    async def get_chat_menu_button(self) -> dict:
        return await self._call("getChatMenuButton", http_timeout=15)

    async def get_updates(self, offset: int | None, timeout: int = 25, *, allowed_updates: list[str] | None = None) -> list[dict]:
        """One long poll. Returns the raw updates, possibly none.

        Only ``message`` updates are asked for: edits, reactions and member
        changes are not part of what he reads. The HTTP timeout runs a little
        past Telegram's so a quiet poll ends on their side, not ours.
        """
        result = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates or ["message"]},
            http_timeout=timeout + 10,
        )
        return list(result or [])

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        """Post to the room. Returns the sent message as Telegram reports it."""
        if len(text) > MESSAGE_MAX_CHARS:
            raise ValueError("Telegram message exceeds 4096 characters; split it before sending")
        return await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "reply_to_message_id": reply_to_message_id, "reply_markup": reply_markup},
            http_timeout=20,
        )

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text}, http_timeout=15)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action}, http_timeout=10)


    async def send_photo(
        self,
        chat_id: str | int,
        path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> dict:
        """Upload one picture from disk. Multipart, unlike every other call here."""
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption[:CAPTION_MAX_CHARS])
        if reply_to_message_id:
            form.add_field("reply_to_message_id", str(reply_to_message_id))
        with open(path, "rb") as handle:
            form.add_field("photo", handle.read(), filename="image.png", content_type="image/png")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._url("sendPhoto"), data=form,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    body = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise TelegramError(0, f"network: {exc}") from exc
        if not isinstance(body, dict) or not body.get("ok"):
            if isinstance(body, dict):
                raise TelegramError(int(body.get("error_code", 0)), str(body.get("description", "")))
            raise TelegramError(0, str(body))
        return body.get("result")


def get_client() -> TelegramClient | None:
    """A client from current settings, or ``None`` when no token is set."""
    from infrastructure.settings_store import load_settings

    token = (load_settings().get("telegram_bot_token") or "").strip()
    if not token:
        return None
    return TelegramClient(token)
