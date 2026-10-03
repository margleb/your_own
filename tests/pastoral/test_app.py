from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from infrastructure.telegram.client import TelegramError
from pastoral_bot.app import BotApp
from pastoral_bot.config import BotSettings
from pastoral_bot.storage import Store, metadata
from pastoral_bot.telegram import split_message
from pastoral_bot.types import Mode, TurnReply


def message(update_id, user_id, text, *, kind="private", chat_id=None):
    return {"update_id": update_id, "message": {"message_id": update_id, "from": {"id": user_id}, "chat": {"id": user_id if chat_id is None else chat_id, "type": kind}, "text": text}}


def callback(update_id, user_id, data):
    return {"update_id": update_id, "callback_query": {"id": f"callback-{update_id}", "from": {"id": user_id}, "message": {"chat": {"id": user_id, "type": "private"}}, "data": data}}


class FakeClient:
    def __init__(self):
        self.sent = []
        self.callbacks = []
        self.failure = None
        self.ack_failure = None

    async def send_message(self, chat_id, text, *, reply_markup=None):
        if self.failure and text.startswith("answer:"):
            failure, self.failure = self.failure, None
            raise failure
        self.sent.append((chat_id, text, reply_markup))
        return {"message_id": len(self.sent)}

    async def answer_callback_query(self, query_id, text=""):
        if self.ack_failure:
            failure, self.ack_failure = self.ack_failure, None
            raise failure
        self.callbacks.append((query_id, text))

    async def send_chat_action(self, chat_id):
        pass


class ImmediateSender:
    """Rate limiting is tested separately; keep dispatcher tests deterministic."""
    def __init__(self, client):
        self.client = client

    async def message(self, chat_id, text, *, keyboard=None):
        return await self.client.send_message(chat_id, text, reply_markup=keyboard)

    async def text(self, chat_id, text, *, keyboard=None):
        for part in split_message(text):
            await self.message(chat_id, part, keyboard=keyboard)


@dataclass
class Gate:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    swallow_cancellation: bool = False


class FakeTurn:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.gates = {}
        self.knowledge = SimpleNamespace(embed=self.embed)

    async def embed(self, text):
        return None

    async def run(self, job, temporary_history=None):
        history = temporary_history if temporary_history is not None else await self.store.history(job.user_id, job.conversation_id)
        self.calls.append((job, list(history)))
        gate = self.gates.get(job.update_id)
        if gate:
            gate.entered.set()
            try:
                await gate.release.wait()
            except asyncio.CancelledError:
                gate.cancelled.set()
                if not gate.swallow_cancellation:
                    raise
                await gate.release.wait()
        return TurnReply("answer:" + job.text)


@pytest_asyncio.fixture
async def app(tmp_path):
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}", tmp_path / "journal.jsonl")
    await store.initialize()
    settings = BotSettings(campaigns="channel_one")
    client = FakeClient()
    instance = BotApp(settings, store, FakeTurn(store), client)
    instance.sender = ImmediateSender(client)
    yield instance
    await instance.close()
    await store.close()


async def idle(app):
    async def wait():
        while app.workers:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 3)


@pytest.mark.asyncio
async def test_private_only_isolated_users_and_duplicate_updates(app):
    await app.handle(message(1, 11, "alice"))
    await app.handle(message(1, 11, "duplicate"))
    await app.handle(message(2, 22, "bob"))
    await app.handle(message(3, 11, "group private marker", kind="supergroup", chat_id=-100))
    await idle(app)
    assert len(app.turn.calls) == 2
    assert {(job.user_id, job.text) for job, _ in app.turn.calls} == {(11, "alice"), (22, "bob")}
    assert {(chat, text) for chat, text, _ in app.client.sent} == {(11, "answer:alice"), (22, "answer:bob")}
    assert await app.store.next_offset() == 4


@pytest.mark.asyncio
async def test_queued_future_text_never_enters_earlier_prompt(app):
    gate = app.turn.gates[1] = Gate()
    await app.handle(message(1, 11, "first"))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await app.handle(message(2, 11, "future"))
    assert app.turn.calls[0][1] == []
    gate.release.set()
    await idle(app)
    assert [job.text for job, _ in app.turn.calls] == ["first", "future"]
    assert app.turn.calls[1][1] == [{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer:first"}]


@pytest.mark.asyncio
async def test_mode_callback_epoch_and_ownership(app):
    state = await app.store.get_user(11)
    await app.handle(callback(1, 11, f"mode:{state.epoch}:confession"))
    assert (await app.store.get_user(11)).mode == Mode.CONFESSION
    await app.handle(callback(2, 11, f"mode:{state.epoch}:talk"))
    assert (await app.store.get_user(11)).mode == Mode.CONFESSION
    assert "устарела" in app.client.callbacks[-1][1]
    forged = callback(3, 22, "mode:0:confession")
    forged["callback_query"]["message"]["chat"]["id"] = 11
    await app.handle(forged)
    assert (await app.store.get_user(22)).mode == Mode.TALK


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["new", "stop", "delete", "mode"])
async def test_control_invalidates_even_a_turn_ignoring_cancellation(app, control):
    gate = app.turn.gates[1] = Gate(swallow_cancellation=True)
    await app.handle(message(1, 11, "late secret"))
    await asyncio.wait_for(gate.entered.wait(), 2)
    state = await app.store.get_user(11)
    if control in ("new", "stop"):
        update = message(2, 11, "/" + control)
    else:
        data = f"delete:{state.epoch}:yes" if control == "delete" else f"mode:{state.epoch}:faith"
        update = callback(2, 11, data)
    action = asyncio.create_task(app.handle(update))
    await asyncio.wait_for(gate.cancelled.wait(), 2)
    gate.release.set()
    await asyncio.wait_for(action, 2)
    await idle(app)
    assert "answer:late secret" not in [text for _, text, _ in app.client.sent]
    assert not await app.store.pending_deliveries()
    assert not await app.store.is_current(app.turn.calls[0][0])


@pytest.mark.asyncio
async def test_temporary_context_not_durable_across_restart(app):
    state = await app.store.get_user(11)
    await app.handle(callback(1, 11, f"mode:{state.epoch}:confession"))
    marker = "TEMP_PRIVATE_RESTART_УНИКАЛЬНЫЙ"
    await app.handle(message(2, 11, marker))
    await idle(app)
    await app.close()
    assert not app.temporary.sessions
    restarted = BotApp(app.settings, app.store, app.turn, app.client)
    restarted.sender = ImmediateSender(app.client)
    try:
        await restarted.recover()
        await restarted.handle(message(3, 11, marker + " next"))
        await idle(restarted)
        assert app.turn.calls[-1][1] == []
        assert (await app.store.get_user(11)).mode == Mode.CONFESSION
        async with app.store.engine.connect() as conn:
            for table in metadata.sorted_tables:
                assert marker not in repr((await conn.execute(select(table))).all())
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_ambiguous_failed_delivery_never_regenerates(app):
    app.client.failure = TelegramError(0, "network_or_decode")
    await app.handle(message(1, 11, "already generated"))
    await idle(app)
    assert len(app.turn.calls) == 1
    await app.recover()
    await idle(app)
    assert len(app.turn.calls) == 1
    assert not any(text == "answer:already generated" for _, text, _ in app.client.sent)


@pytest.mark.asyncio
async def test_generated_delivery_resume_skips_acknowledged_chunks(app):
    job = await app.store.accept_message(1, 11, 11, "question")
    text = "answer:" + "а"*7500
    assert await app.store.complete(job, text)
    await app.store.mark_delivered(1, 1)
    await app.recover()
    assert not app.turn.calls
    assert [text for _, text, _ in app.client.sent] == split_message(text)[1:]
    assert await app.store.delivery_state(1) == 3
    assert not await app.store.pending_deliveries()


@pytest.mark.asyncio
async def test_shutdown_preserves_active_ordinary_job_for_restart(app):
    gate = app.turn.gates[1] = Gate()
    await app.handle(message(1, 11, "interrupted by restart"))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await app.close()
    pending = await app.store.pending_jobs()
    assert [job.update_id for job in pending] == [1]
    app.turn.gates.pop(1)
    restarted = BotApp(app.settings, app.store, app.turn, app.client)
    restarted.sender = ImmediateSender(app.client)
    try:
        await restarted.recover()
        await idle(restarted)
        assert any(text == "answer:interrupted by restart" for _, text, _ in app.client.sent)
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_callback_ack_failure_cannot_revert_confession_mode(app):
    state = await app.store.get_user(11)
    app.client.ack_failure = TelegramError(0, "ack_network")
    update = callback(1, 11, f"mode:{state.epoch}:confession")
    with pytest.raises(TelegramError):
        await app.handle(update)
    assert (await app.store.get_user(11)).mode == Mode.CONFESSION
    await app.handle(update)
    await app.handle(message(2, 11, "PRIVATE_AFTER_ACK_FAILURE"))
    await idle(app)
    assert app.turn.calls[-1][0].mode == Mode.CONFESSION
    assert app.turn.calls[-1][0].message_id is None
    async with app.store.engine.connect() as conn:
        for table in metadata.sorted_tables:
            assert "PRIVATE_AFTER_ACK_FAILURE" not in repr((await conn.execute(select(table))).all())


@pytest.mark.asyncio
async def test_confession_queue_expires_while_global_slots_are_busy(app):
    app.settings.temporary_idle_seconds = 1
    await app.store.set_mode(11, Mode.CONFESSION)
    for _ in range(app.settings.concurrency):
        await app.semaphore.acquire()
    try:
        await app.handle(message(1, 11, "EXPIRED_QUEUE_PRIVATE"))
        await idle(app)
        assert not app.turn.calls
        assert not app.temporary.sessions
        assert not app.arrivals
        assert (await app.store.get_user(11)).mode == Mode.CONFESSION
        async with app.store.engine.connect() as conn:
            for table in metadata.sorted_tables:
                assert "EXPIRED_QUEUE_PRIVATE" not in repr((await conn.execute(select(table))).all())
    finally:
        for _ in range(app.settings.concurrency):
            app.semaphore.release()
