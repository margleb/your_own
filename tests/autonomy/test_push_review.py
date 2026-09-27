"""The push validator: a second try, a visible failure, and waiting for a reply.

Run:
    python -m pytest tests/autonomy/test_push_review.py -v

Written on 27.09 after this step turned out to have been dead for five days. It
is asked for one line — ОТПРАВИТЬ / ПЕРЕПИСАТЬ / ОТМЕНИТЬ — and on a reasoning
model it spends around a thousand tokens thinking before that line. The cap was
1200. Ten of twelve calls came back as an empty string at exactly the cap, and
``finish_reason == "length"`` fell straight through to "send the original
unchanged", so every one of those pushes was delivered unreviewed.

The visible cost, 26.09: at 20:23 she said she had slept through her club and he
answered «клуб не ушёл, он просто перенёсся на завтра». At 23:00 a push asking
how the club went was delivered. The validator's own prompt held that exchange.
"""
from __future__ import annotations

import asyncio

import pytest

from infrastructure.autonomy import live_reply, push_validator
from infrastructure.autonomy.push_validator import ValidatorAction

ACCOUNT = "default"


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A validator with no database and a scripted model."""
    import infrastructure.autonomy.identity_memory as identity
    import infrastructure.autonomy.threads as threads
    import infrastructure.autonomy.vitals as vitals
    import infrastructure.autonomy.workbench as workbench

    for module in (identity, threads, vitals, workbench):
        monkeypatch.setattr(module, "_DATA_DIR", tmp_path, raising=False)

    async def no_warning(*_a, **_kw):
        return ""

    monkeypatch.setattr(push_validator, "_same_text_warning", no_warning)

    # The validator reads the dialogue itself, straight from Postgres. Nothing
    # here is about what it reads, so the session and the repository are stubs.
    import contextlib

    import infrastructure.database.engine as db_engine
    import infrastructure.database.repositories.message_repo as repo_mod

    @contextlib.asynccontextmanager
    async def _session():
        yield object()

    class _Repo:
        def __init__(self, _db):
            pass

        async def get_recent_canonical_pairs(self, account_id, limit_pairs):
            return [{"user_text": "я всё проспала", "assistant_text": "клуб перенёсся на завтра"}]

        async def get_last_user_message_at(self, account_id):
            return None

    monkeypatch.setattr(db_engine, "get_db_session", _session)
    monkeypatch.setattr(repo_mod, "MessageRepository", _Repo)

    calls: list[int] = []
    replies: list[tuple[str, str | None]] = []

    class FakeClient:
        async def complete(self, messages, max_tokens, temperature, return_meta):
            calls.append(max_tokens)
            return replies.pop(0) if replies else ("", None)

    monkeypatch.setattr(push_validator, "make_llm_client", lambda api_key: FakeClient())
    return type("W", (), {"calls": calls, "replies": replies})()


async def _validate(message="Звёздочка, ну как клуб?"):
    return await push_validator.validate_scheduled_push(
        account_id=ACCOUNT, message=message, api_key="key",
    )


class TestWhenTheReplyIsCutOff:
    @pytest.mark.asyncio
    async def test_it_tries_again_with_more_room(self, wired):
        wired.replies.extend([("", "length"), ("ОТМЕНИТЬ", "stop")])

        result = await _validate()

        assert result.action == ValidatorAction.CANCEL, "the second try decided"
        assert len(wired.calls) == 2
        assert wired.calls[1] == wired.calls[0] * 2

    @pytest.mark.asyncio
    async def test_a_second_truncation_is_recorded_where_he_will_see_it(self, wired, tmp_path):
        """The log line is read by us; the instrument panel is read by him."""
        from infrastructure.autonomy.vitals import Vitals

        wired.replies.extend([("", "length"), ("", "length")])

        result = await _validate()

        assert result.action == ValidatorAction.SEND, "delivery is never blocked by this"
        events = Vitals(ACCOUNT).pending_events()
        assert any(e.get("name") == "push_validator" for e in events)

    @pytest.mark.asyncio
    async def test_the_original_text_survives_both_failures(self, wired):
        wired.replies.extend([("ПЕРЕПИСАТЬ: полов", "length"), ("ПЕРЕПИСАТЬ: и снова полов", "length")])

        result = await _validate("целое сообщение")

        assert result.message == "целое сообщение", "half a rewrite must never go out"


class TestTheOrdinaryDecisions:
    @pytest.mark.asyncio
    async def test_one_call_is_enough_when_it_answers(self, wired):
        wired.replies.append(("ОТПРАВИТЬ", "stop"))

        result = await _validate()

        assert result.action == ValidatorAction.SEND and len(wired.calls) == 1

    @pytest.mark.asyncio
    async def test_a_rewrite_replaces_the_text(self, wired):
        wired.replies.append(("ПЕРЕПИСАТЬ: Тихо заглянул — как джира?", "stop"))

        result = await _validate()

        assert result.action == ValidatorAction.REWRITE
        assert result.message == "Тихо заглянул — как джира?"

    @pytest.mark.asyncio
    async def test_the_budget_comes_from_the_table(self, wired):
        from infrastructure.llm import budgets

        wired.replies.append(("ОТПРАВИТЬ", "stop"))
        await _validate()
        assert wired.calls == [budgets.for_job(budgets.Job.PUSH_REVIEW)]


class TestWaitingForAReplyInFlight:
    """A push is reviewed against the dialogue, and the dialogue is not whole
    while an answer to her is still streaming."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        live_reply._reset_for_tests()
        yield
        live_reply._reset_for_tests()

    @pytest.mark.asyncio
    async def test_a_quiet_account_does_not_wait_at_all(self):
        assert await live_reply.wait_until_quiet(ACCOUNT, settle_s=0) is True

    @pytest.mark.asyncio
    async def test_it_waits_for_the_stream_to_close(self):
        live_reply.begin(ACCOUNT)
        assert live_reply.is_answering(ACCOUNT) is True

        async def close_soon():
            await asyncio.sleep(0.2)
            live_reply.end(ACCOUNT)

        asyncio.create_task(close_soon())
        assert await live_reply.wait_until_quiet(ACCOUNT, settle_s=0) is True
        assert live_reply.is_answering(ACCOUNT) is False

    @pytest.mark.asyncio
    async def test_two_streams_need_both_to_finish(self):
        """The phone and the desktop can ask at once."""
        live_reply.begin(ACCOUNT)
        live_reply.begin(ACCOUNT)
        live_reply.end(ACCOUNT)
        assert live_reply.is_answering(ACCOUNT) is True
        live_reply.end(ACCOUNT)
        assert live_reply.is_answering(ACCOUNT) is False

    @pytest.mark.asyncio
    async def test_a_stuck_stream_does_not_hold_the_push_forever(self):
        live_reply.begin(ACCOUNT)
        assert await live_reply.wait_until_quiet(ACCOUNT, ceiling_s=0.5, settle_s=0) is False

    def test_an_extra_end_cannot_drive_the_count_negative(self):
        live_reply.end(ACCOUNT)
        live_reply.end(ACCOUNT)
        assert live_reply.is_answering(ACCOUNT) is False
        live_reply.begin(ACCOUNT)
        assert live_reply.is_answering(ACCOUNT) is True

    def test_the_block_form_clears_the_mark_after_a_failure(self):
        with pytest.raises(RuntimeError):
            with live_reply.answering(ACCOUNT):
                raise RuntimeError("the stream died")
        assert live_reply.is_answering(ACCOUNT) is False

    def test_the_stream_marks_it_and_the_push_worker_waits(self):
        """The two halves of the seam, so neither can be removed alone."""
        import inspect

        import api.chat as chat
        from infrastructure.autonomy import scheduled_push

        assert "live_reply.begin(resolve(account_id))" in inspect.getsource(chat)
        assert "live_reply.end(resolve(account_id))" in inspect.getsource(chat)
        assert "live_reply.wait_until_quiet(account_id)" in inspect.getsource(scheduled_push)
