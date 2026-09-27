"""How many completion tokens each job may spend, per model.

A cap is not a bill. You pay for the tokens a model generates, not for the
ceiling you allowed it. What a cap decides is whether the answer arrives whole
— and a cap set too low does not shorten the answer, it deletes it.

Every number here used to be a constant at its call site, chosen when the model
in settings was ``~anthropic/claude-fable-latest``, which writes its answer
straight out. The model in settings on 27.09 is ``~moonshotai/kimi-latest``,
which reasons first and spends the same budget doing it. Measured on the live
server over the week of 21–27.09:

    truncated at max_tokens=650    21 times   memory dedup — every one lost
    truncated at max_tokens=1200   11 times   the push validator
    truncated at max_tokens=16000   4 times   genuinely long answers

The push validator is the clearest case. It is asked for one line —
``ОТПРАВИТЬ`` / ``ПЕРЕПИСАТЬ: …`` / ``ОТМЕНИТЬ``. On the two occasions in five
days that it answered at all it spent 1007 and 1078 completion tokens getting
there, against a cap of 1200. The other ten came back as an empty string at
exactly the cap, which the code read as "reply truncated, send the original
unvalidated" — so on 26.09 a push asking how her club went was delivered three
hours after she had told him she slept through it, with his own words to that
effect sitting in the validator's prompt.

So a budget is two numbers added: the size of the visible answer, which belongs
to the job, and room for the model to think before it, which belongs to the
model. Raising the first is a decision about the work. Raising the second costs
nothing until a model actually uses it.

Usage::

    from infrastructure.llm import budgets

    max_tokens=budgets.for_job(budgets.Job.PUSH_REVIEW)
"""
from __future__ import annotations

from enum import Enum


class Job(str, Enum):
    """What the model is being asked to produce, sized by the answer itself."""

    #: One word or one line: memory dedup's verdict, the research judge's.
    VERDICT = "verdict"
    #: One line, or one rewritten push message.
    PUSH_REVIEW = "push_review"
    #: Prose assembled from server-side web tools, with citations.
    WEB_ANSWER = "web_answer"
    #: Nicknames seeded for the room, one per line.
    ALIASES = "aliases"
    #: A research brief over retrieved material.
    BRIEF = "brief"
    #: An answer read out of the project's own documentation.
    DOC_ANSWER = "doc_answer"
    #: A reply in the group chat, commands included.
    ROOM_REPLY = "room_reply"
    #: His journal entry after an exchange, commands included.
    JOURNAL = "journal"
    #: One step of a reflection, or one step of the rotator.
    STEP = "step"


#: What the visible answer needs, before the model thinks.
#:
#: Every number is the old call-site constant, never less: a model that does not
#: reason must come out of this change with exactly the budget it had. What is
#: added for one that does reason is :data:`THINKING_ROOM`, and nothing else.
VISIBLE: dict[Job, int] = {
    Job.VERDICT: 800,
    Job.PUSH_REVIEW: 1200,
    Job.WEB_ANSWER: 1500,
    Job.ALIASES: 4000,
    Job.BRIEF: 6000,
    Job.DOC_ANSWER: 8000,
    Job.ROOM_REPLY: 8000,
    Job.JOURNAL: 16000,
    Job.STEP: 16000,
}

#: Room a model needs to think before its first visible token, by the longest
#: matching prefix of the model id. A reasoning model spends the completion
#: budget on reasoning, and OpenRouter counts that against ``max_tokens``.
#:
#: Measured, not guessed: kimi spent ~1050 tokens reaching a one-line verdict,
#: so 2500 is that with margin. Anthropic's models are at zero because these
#: numbers worked for claude-fable for months, and nothing here turns extended
#: thinking on.
THINKING_ROOM: dict[str, int] = {
    "anthropic/": 0,
    "moonshotai/": 2500,
    "deepseek/": 2500,
    "qwen/": 2000,
    "z-ai/": 2500,
    "openai/o1": 3000,
    "openai/o3": 3000,
    "openai/": 0,
    "google/gemini-2.5": 2500,
    "google/": 0,
}

#: A model nobody has measured. It costs nothing to assume it thinks, and
#: everything to assume it does not — that assumption is what this file is for.
DEFAULT_THINKING_ROOM = 2500


def _normalise(model: str) -> str:
    """``~moonshotai/kimi-latest`` → ``moonshotai/kimi-latest``.

    The leading ``~`` is this project's own marker for "resolve to the newest
    build of this model", not part of the id.
    """
    return (model or "").strip().lstrip("~").lower()


def thinking_room(model: str | None = None) -> int:
    """How much this model burns before its answer starts."""
    name = _normalise(model if model is not None else _model_from_settings())
    if not name:
        return DEFAULT_THINKING_ROOM
    # Longest prefix wins, so "openai/o1" beats "openai/".
    best: int | None = None
    best_len = -1
    for prefix, room in THINKING_ROOM.items():
        if name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = room, len(prefix)
    return DEFAULT_THINKING_ROOM if best is None else best


def for_job(job: Job, model: str | None = None) -> int:
    """The cap for *job* on *model* — the answer plus room to think.

    *model* defaults to the one in settings, which is the one every caller here
    is about to use.
    """
    return VISIBLE[job] + thinking_room(model)


def _model_from_settings() -> str:
    try:
        from infrastructure.settings_store import DEFAULT_MODEL, load_settings

        return str(load_settings().get("model") or DEFAULT_MODEL)
    except Exception:       # settings unreadable: assume the model thinks
        return ""
