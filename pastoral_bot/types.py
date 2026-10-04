"""Small, transport-independent contracts for the isolated bot."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Mode(str, Enum):
    TALK = "talk"
    FAITH = "faith"
    CONFESSION = "confession"


@dataclass(frozen=True)
class UserState:
    user_id: int
    mode: Mode
    conversation_id: str
    epoch: int


@dataclass(frozen=True)
class TurnJob:
    update_id: int
    user_id: int
    chat_id: int
    conversation_id: str
    epoch: int
    mode: Mode
    text: str = field(repr=False)
    message_id: int | None = None
    notice: str = ""
    channel: str = "telegram"
    request_id: str | None = None


@dataclass(frozen=True)
class SourcePassage:
    source_id: str
    title: str
    edition: str
    locator: str
    url: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class TurnReply:
    text: str = field(repr=False)
    source_ids: list[str] = field(default_factory=list)
    referral: str | None = None
    body: str | None = field(default=None, repr=False)
    sources: list[SourcePassage] = field(default_factory=list, repr=False)
