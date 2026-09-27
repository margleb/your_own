"""Is he answering her right now?

A scheduled push is reviewed against the dialogue before it goes out, and the
dialogue is only whole once the answer being written has landed in it. Without
this the validator can read a conversation that is one exchange out of date —
the exchange still streaming to her screen — and wave through a question she is
in the middle of answering.

An in-process counter is the right shape here and not a shortcut: one backend is
enforced at the process boundary (``infrastructure/single_process.py``), and the
stream and the push worker are two tasks in that one process. A flag in a file
would buy nothing and could go stale after a crash; a counter held in memory
cannot outlive the streams it counts.

A counter rather than a boolean because two clients can ask at once — the phone
and the desktop — and the second stream closing must not clear the first.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator

from infrastructure.clock import now_utc
from infrastructure.logging.logger import setup_logger

logger = setup_logger("autonomy.live_reply")

#: How long to wait for a reply in flight before giving up on it. A stream that
#: has run this long is stuck, and a push held hostage to it is worse than a
#: push reviewed against a slightly stale dialogue.
WAIT_CEILING_S = 90.0

#: After the last stream closes, the pair still has to be committed and read
#: back. Long enough for that write, short enough that nobody notices.
SETTLE_S = 1.5

_open: defaultdict[str, int] = defaultdict(int)
_last_closed: dict[str, float] = {}


def begin(account_id: str) -> None:
    """A reply to her has started streaming."""
    _open[account_id] += 1


def end(account_id: str) -> None:
    """That reply has finished, one way or another.

    Never raises: it runs in the stream's ``finally``, and an exception there
    would replace whatever actually went wrong.
    """
    try:
        _open[account_id] = max(0, _open[account_id] - 1)
        if _open[account_id] == 0:
            _last_closed[account_id] = now_utc().timestamp()
    except Exception as exc:        # pragma: no cover - a dict cannot do this
        logger.error("[live_reply:%s] could not clear the mark: %s", account_id, exc)


@contextmanager
def answering(account_id: str) -> Iterator[None]:
    """:func:`begin` and :func:`end` as a block, for callers that can nest one."""
    begin(account_id)
    try:
        yield
    finally:
        end(account_id)


def is_answering(account_id: str) -> bool:
    """True while at least one reply to her is still streaming."""
    return _open.get(account_id, 0) > 0


async def wait_until_quiet(
    account_id: str,
    *,
    ceiling_s: float = WAIT_CEILING_S,
    settle_s: float = SETTLE_S,
) -> bool:
    """Wait for any reply in flight to finish. True if it went quiet in time.

    Returns False when the ceiling ran out with a stream still open — the
    caller then proceeds on a dialogue that may be one exchange short, which is
    the same position it was always in before this existed.
    """
    if not is_answering(account_id):
        return True

    logger.info("[live_reply:%s] a reply is streaming — holding the push", account_id)
    waited = 0.0
    while is_answering(account_id) and waited < ceiling_s:
        await asyncio.sleep(0.5)
        waited += 0.5

    if is_answering(account_id):
        logger.warning(
            "[live_reply:%s] still streaming after %.0fs — going ahead anyway",
            account_id, waited,
        )
        return False

    # The reply is written but the row may not be readable yet.
    await asyncio.sleep(settle_s)
    logger.info("[live_reply:%s] the reply landed after %.1fs — validating now", account_id, waited)
    return True


def _reset_for_tests() -> None:
    _open.clear()
    _last_closed.clear()
