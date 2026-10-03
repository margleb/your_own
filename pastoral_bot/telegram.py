"""Bounded, plain text delivery. Never retries ambiguous sends or regenerates."""
from __future__ import annotations

import asyncio
import time

from infrastructure.telegram.client import TelegramClient, TelegramError


def split_message(text: str, limit: int = 3500) -> list[str]:
    if limit < 1:
        raise ValueError("limit must be positive")
    # Telegram counts UTF-16 units, so an emoji occupies two units.
    parts: list[str] = []
    rest = text
    while rest:
        units = 0
        end = 0
        for char in rest:
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                break
            end += 1
        if end == len(rest):
            parts.append(rest)
            break
        if not end:
            raise ValueError("limit too small for character")
        boundary = rest.rfind("\n\n", 0, end)
        if boundary > end // 2:
            end = boundary + 2
        parts.append(rest[:end])
        rest = rest[end:]
    return parts


class Sender:
    def __init__(self, client: TelegramClient):
        self.client = client
        self._global = asyncio.Lock()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last_chat: dict[int, float] = {}
        self._last_global = 0.0

    async def message(self, chat_id: int, text: str, *, keyboard: dict | None = None) -> dict:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            for attempt in range(3):
                await asyncio.sleep(max(0, self._last_chat.get(chat_id, 0) + 1.05 - time.monotonic()))
                async with self._global:
                    await asyncio.sleep(max(0, self._last_global + .05 - time.monotonic()))
                    self._last_global = time.monotonic()
                self._last_chat[chat_id] = time.monotonic()
                try:
                    return await self.client.send_message(chat_id, text, reply_markup=keyboard)
                except TelegramError as exc:
                    if exc.status != 429 or attempt == 2:
                        raise
                    await asyncio.sleep(max(1, float(exc.retry_after or 1)))
        raise TelegramError(429, "delivery_limit")

    async def text(self, chat_id: int, text: str, *, keyboard: dict | None = None) -> None:
        parts = split_message(text)
        for index, part in enumerate(parts):
            await self.message(chat_id, part, keyboard=keyboard if index == len(parts) - 1 else None)
