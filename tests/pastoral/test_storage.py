from __future__ import annotations

import os
import shutil
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from pastoral_bot.storage import Store, chunks, jobs, messages, metadata
from pastoral_bot.types import Mode


@pytest_asyncio.fixture
async def store(tmp_path):
    instance = Store(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}", tmp_path / "journal" / "deletions.jsonl")
    await instance.initialize()
    yield instance
    await instance.close()


@pytest.mark.asyncio
async def test_deduplication_and_completed_history_only(store):
    job = await store.accept_message(10, 1, 1, "first")
    future = await store.accept_message(11, 1, 1, "future")
    assert await store.accept_message(10, 1, 1, "duplicate") is None
    assert await store.history(1, job.conversation_id) == []
    assert await store.complete(job, "answer")
    assert await store.history(1, job.conversation_id) == [dict(role="user", content="first"), dict(role="assistant", content="answer")]
    assert [job.update_id for job in await store.pending_jobs()] == [future.update_id]
    assert await store.next_offset() == 12
    assert await store.accept_control(12)
    assert not await store.accept_control(12)
    assert await store.next_offset() == 13


@pytest.mark.asyncio
async def test_isolation_history_recall_and_forged_message_reference(store):
    alice = await store.accept_message(1, 101, 101, "Alice private")
    bob = await store.accept_message(2, 202, 202, "Bob private")
    assert await store.complete(replace(alice, message_id=bob.message_id), "Alice reply", [1.0] + [0.0]*383)
    assert await store.history(202, alice.conversation_id) == []
    assert await store.complete(bob, "Bob reply", [1.0] + [0.0]*383)
    state = await store.new_conversation(101)
    recalled = await store.recall(101, state.conversation_id, [1.0] + [0.0]*383)
    assert [row["content"] for row in recalled] == ["Alice private", "Alice reply"]
    assert "Bob" not in repr(recalled)


@pytest.mark.asyncio
async def test_confession_never_durable_even_after_restart(store, tmp_path):
    await store.set_mode(3, Mode.CONFESSION)
    marker = "CONFESSION_PRIVATE_SENTINEL_УНИКАЛЬНЫЙ"
    job = await store.accept_message(20, 3, 3, marker)
    assert await store.complete(job, marker + " response", [1.0]*384)
    assert not await store.pending_jobs()
    assert not await store.pending_deliveries()
    async with store.engine.connect() as conn:
        for table in metadata.sorted_tables:
            rows = (await conn.execute(select(table))).all()
            assert marker not in repr(rows), table.name
    await store.close()
    reloaded = Store(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    try:
        state = await reloaded.get_user(3)
        assert state.mode == Mode.CONFESSION
        assert await reloaded.history(3, state.conversation_id) == []
        assert marker.encode() not in (tmp_path / "bot.db").read_bytes()
    finally:
        await reloaded.close()


@pytest.mark.asyncio
async def test_delete_epoch_guard_and_search_removal(store):
    job = await store.accept_message(1, 1, 1, "erase me")
    assert await store.complete(job, "erase reply", [1.0]*384)
    late = await store.accept_message(2, 1, 1, "pending")
    state = await store.delete_history(1)
    assert not await store.is_current(late)
    assert not await store.complete(late, "late response", [1.0]*384)
    assert await store.history(1, state.conversation_id) == []
    assert await store.recall(1, state.conversation_id, [1.0]*384) == []
    async with store.engine.connect() as conn:
        for table in (messages, chunks, jobs):
            assert not (await conn.execute(select(table))).all()
    assert "erase" not in store.deletion_journal.read_text()


@pytest.mark.asyncio
async def test_delivery_recovery_and_mode_change(store):
    job = await store.accept_message(1, 1, 1, "question")
    assert await store.complete(job, "validated full answer")
    assert not await store.complete(job, "duplicate")
    await store.mark_delivered(1, 2)
    await store.mark_delivered(1, 1)
    restored = await store.pending_deliveries()
    assert len(restored) == 1
    assert restored[0][1:] == ("validated full answer", 2)
    assert await store.delivery_state(1) == 2
    await store.set_mode(1, Mode.FAITH)
    assert not await store.pending_deliveries()
    assert not await store.is_current(job)


@pytest.mark.asyncio
async def test_deletion_journal_replays_after_restore_without_deleting_newer_history(tmp_path):
    path = tmp_path / "bot.db"
    journal = tmp_path / "outside-backups" / "journal.jsonl"
    store = Store(f"sqlite+aiosqlite:///{path}", journal)
    await store.initialize()
    old = await store.accept_message(1, 1, 1, "old personal text")
    await store.complete(old, "old reply")
    await store.close()
    snapshot = tmp_path / "before-delete.db"
    shutil.copy2(path, snapshot)
    store = Store(f"sqlite+aiosqlite:///{path}", journal)
    await store.delete_history(1)
    new = await store.accept_message(2, 1, 1, "new personal text")
    await store.complete(new, "new reply")
    await store.replay_deletions()
    assert [row["content"] for row in await store.history(1, new.conversation_id)] == ["new personal text", "new reply"]
    await store.close()
    shutil.copy2(snapshot, path)
    restored = Store(f"sqlite+aiosqlite:///{path}", journal)
    try:
        await restored.replay_deletions()
        state = await restored.get_user(1)
        assert state.epoch > old.epoch
        assert await restored.history(1, old.conversation_id) == []
        assert not await restored.complete(old, "resurrected reply")
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_campaign_first_touch_survives_history_deletion(store):
    await store.attribute(1, "first_channel")
    await store.attribute(1, "second_channel")
    job = await store.accept_message(1, 1, 1, "message")
    await store.complete(job, "answer")
    await store.delete_history(1)
    metrics = await store.metrics()
    assert metrics == [{"campaign": "first_channel", "starts": 1, "first_answers": 1, "returned_users": 0}]


@pytest.mark.asyncio
async def test_atomic_callback_duplicate_stale_and_delete(store):
    state = await store.get_user(1)
    changed = await store.transition_callback(10, 1, state.epoch, mode=Mode.CONFESSION)
    assert changed.mode == Mode.CONFESSION
    assert await store.transition_callback(10, 1, state.epoch, mode=Mode.TALK) is None
    assert await store.transition_callback(11, 1, state.epoch, mode=Mode.TALK) is None
    assert (await store.get_user(1)).mode == Mode.CONFESSION
    state = await store.set_mode(1, Mode.TALK)
    job = await store.accept_message(12, 1, 1, "delete this")
    await store.complete(job, "reply")
    deleted = await store.transition_callback(13, 1, state.epoch, delete_history=True)
    assert deleted.epoch > state.epoch
    assert await store.history(1, job.conversation_id) == []
    assert await store.next_offset() == 14


@pytest.mark.asyncio
async def test_mode_journal_restores_present_and_absent_users(tmp_path):
    path = tmp_path / "bot.db"
    journal = tmp_path / "outside-backups" / "modes.jsonl"
    store = Store(f"sqlite+aiosqlite:///{path}", mode_journal=journal)
    await store.initialize()
    before = await store.get_user(1)
    await store.close()
    snapshot = tmp_path / "normal.db"
    shutil.copy2(path, snapshot)
    current = Store(f"sqlite+aiosqlite:///{path}", mode_journal=journal)
    await current.set_mode(1, Mode.CONFESSION)
    await current.set_mode(2, Mode.CONFESSION)  # absent from the older snapshot
    assert all(set(__import__("json").loads(line)) == {"user_id", "mode"} for line in journal.read_text().splitlines())
    await current.close()
    shutil.copy2(snapshot, path)
    restored = Store(f"sqlite+aiosqlite:///{path}", mode_journal=journal)
    try:
        await restored.replay_modes()
        one = await restored.get_user(1)
        two = await restored.get_user(2)
        assert one.mode == two.mode == Mode.CONFESSION
        assert one.epoch > before.epoch
        await restored.replay_modes()
        assert (await restored.get_user(1)).epoch == one.epoch
        first = await restored.accept_message(100, 1, 1, "restore private marker")
        second = await restored.accept_message(101, 2, 2, "restore private marker")
        assert first.message_id is second.message_id is None
        assert await restored.pending_jobs() == []
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_postgres_vector_store_in_disposable_database():
    """Opt-in integration: only a freshly created random pastoral_test_* database."""
    if os.environ.get("PASTORAL_TEST_POSTGRES") != "1":
        pytest.skip("Set PASTORAL_TEST_POSTGRES=1 to run isolated PostgreSQL integration")
    import asyncpg
    from sqlalchemy.engine import make_url
    from settings import settings
    url = make_url(settings.DATABASE_URL)
    dbname = "pastoral_test_" + uuid4().hex
    admin = await asyncpg.connect(user=url.username, password=url.password, host=url.host, port=url.port or 5432, database="postgres")
    await admin.execute(f'CREATE DATABASE "{dbname}"')
    instance = Store(url.set(database=dbname).render_as_string(hide_password=False))
    try:
        await instance.initialize()
        alice = await instance.accept_message(1, 1, 1, "private alice")
        bob = await instance.accept_message(2, 2, 2, "private bob")
        await instance.complete(alice, "alice reply", [1.0]+[0.0]*383)
        await instance.complete(bob, "bob reply", [1.0]+[0.0]*383)
        state = await instance.new_conversation(1)
        assert [row["content"] for row in await instance.recall(1, state.conversation_id, [1.0]+[0.0]*383)] == ["private alice", "alice reply"]
        await instance.delete_history(1)
        assert await instance.recall(1, state.conversation_id, [1.0]+[0.0]*383) == []
        from pastoral_bot.budget import Budget
        from decimal import Decimal
        import asyncio
        # Two independent Store locks exercise actual PostgreSQL row locking,
        # rather than relying on our single-process asyncio lock.
        second = Store(url.set(database=dbname).render_as_string(hide_password=False))
        try:
            budgets = [Budget(instance), Budget(second)]
            results = await asyncio.gather(*(budgets[n % 2].reserve(500+n, Decimal("1")) for n in range(30)))
            assert sum(result is not None for result in results) == 5
            for result in results:
                if result:
                    await budgets[0].settle(result, Decimal("1"), answered=True)
            assert (await budgets[0].usage(1))["spent"] == Decimal("5")
        finally:
            await second.close()
    finally:
        await instance.close()
        # dbname is generated above; never drop a configured/user database.
        await admin.execute(f'DROP DATABASE "{dbname}"')
        await admin.close()
