"""Confession preparation lives solely in RAM, including pending context."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Session:
    epoch: int
    started: float
    touched: float
    messages: list[dict] = field(default_factory=list, repr=False)
    session_id: str = field(default_factory=lambda: uuid4().hex)
    next_message_id: int = 0


class TemporarySessions:
    def __init__(self, idle_seconds: int = 1800, max_seconds: int = 7200, clock=time.monotonic):
        self.idle_seconds = idle_seconds
        self.max_seconds = max_seconds
        self.clock = clock
        self.sessions: dict[int, Session] = {}

    def prune(self) -> None:
        now = self.clock()
        for user_id, session in list(self.sessions.items()):
            if now - session.touched >= self.idle_seconds or now - session.started >= self.max_seconds:
                self.clear(user_id)

    def context(self, user_id: int, epoch: int) -> tuple[list[dict], bool]:
        self.prune()
        now = self.clock()
        session = self.sessions.get(user_id)
        fresh = session is None or session.epoch != epoch
        if fresh:
            self.clear(user_id)
            session = Session(epoch, now, now)
            self.sessions[user_id] = session
        session.touched = now
        return [{"role": item["role"], "content": item["content"]} for item in session.messages], fresh

    def start(self, user_id: int, epoch: int) -> None:
        self.clear(user_id)
        now = self.clock()
        self.sessions[user_id] = Session(epoch, now, now)

    def peek(self, user_id: int, epoch: int) -> list[dict] | None:
        """Read/polling never extends the temporary session's lifetime."""
        self.prune()
        session = self.sessions.get(user_id)
        if session is None or session.epoch != epoch:
            return None
        return list(session.messages)

    def touch(self, user_id: int, epoch: int) -> bool:
        if self.peek(user_id, epoch) is None:
            return False
        self.sessions[user_id].touched = self.clock()
        return True

    def remaining(self, user_id: int, epoch: int) -> float:
        if self.peek(user_id, epoch) is None:
            return 0
        session = self.sessions[user_id]
        now = self.clock()
        return max(0, min(self.idle_seconds - (now - session.touched), self.max_seconds - (now - session.started)))

    def append(self, user_id: int, epoch: int, question: str, answer: str, *, reply_metadata: dict | None = None) -> None:
        session = self.sessions.get(user_id)
        if session is None or session.epoch != epoch:
            return
        prefix = f"temporary-{session.session_id}-"
        session.messages.extend([
            {"id": prefix + str(session.next_message_id), "role": "user", "content": question},
            {"id": prefix + str(session.next_message_id + 1), "role": "assistant", "content": answer,
             **({"reply_metadata": reply_metadata} if reply_metadata else {})},
        ])
        session.next_message_id += 2
        session.messages[:] = session.messages[-16:]
        session.touched = self.clock()

    def clear(self, user_id: int) -> None:
        session = self.sessions.pop(user_id, None)
        if session:
            session.messages.clear()
