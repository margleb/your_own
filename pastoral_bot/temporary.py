"""Confession preparation lives solely in RAM, including pending context."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Session:
    epoch: int
    started: float
    touched: float
    messages: list[dict] = field(default_factory=list, repr=False)


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
        return list(session.messages), fresh

    def append(self, user_id: int, epoch: int, question: str, answer: str) -> None:
        session = self.sessions.get(user_id)
        if session is None or session.epoch != epoch:
            return
        session.messages.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
        session.messages[:] = session.messages[-16:]
        session.touched = self.clock()

    def clear(self, user_id: int) -> None:
        session = self.sessions.pop(user_id, None)
        if session:
            session.messages.clear()
