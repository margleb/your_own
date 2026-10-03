from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from pastoral_bot.backup import BackupError, create_backup, prune, restore_backup
from pastoral_bot.config import BotSettings
from pastoral_bot.telegram import split_message


def test_long_text_and_emojis_are_delivered_without_loss():
    original = ("Привет 🌿" * 1500) + "\n\n" + "я" * 9000
    parts = split_message(original)
    assert "".join(parts) == original
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)
    assert len(parts) > 2


@pytest.mark.asyncio
async def test_snapshot_never_writes_plaintext_and_restore_replays_both_journals(tmp_path, monkeypatch):
    from pastoral_bot import backup
    marker = b"PRIVATE_ORDINARY_CONTENT_\xd0\xa1\xd0\xb5\xd0\xba\xd1\x80\xd0\xb5\xd1\x82"
    settings = BotSettings(database_url="postgresql+asyncpg://pastoral:secret@localhost/pastoral",
                           state_dir=tmp_path, backup_key=Fernet.generate_key().decode())
    calls = []

    async def command(args, env, payload=None):
        calls.append((Path(args[0]).name, payload))
        assert "secret" not in " ".join(args)
        if args[0] == "pg_dump":
            assert "--table=public.pastoral_*" in args
            assert "--strict-names" in args
        return marker if args[0] == "pg_dump" else b""

    monkeypatch.setattr(backup, "_command", command)
    snapshot = await create_backup(settings)
    assert marker not in snapshot.read_bytes()
    assert snapshot.stat().st_mode & 0o077 == 0
    assert list(tmp_path.glob("backups/*")) == [snapshot]
    events = []

    class FakeStore:
        deletion_journal = tmp_path / "deletions.jsonl"
        mode_journal = tmp_path / "modes.jsonl"

        async def initialize(self):
            events.append("initialize")

        async def replay_deletions(self):
            events.append("deletions")

        async def replay_modes(self):
            events.append("modes")

    store = FakeStore()
    with pytest.raises(BackupError):
        await restore_backup(settings, snapshot, store)
    assert len(calls) == 1  # no destructive command without preserved journals
    store.deletion_journal.touch()
    store.mode_journal.touch()
    await restore_backup(settings, snapshot, store)
    assert calls[-1] == ("pg_restore", marker)
    assert events == ["initialize", "initialize", "deletions", "modes"]


@pytest.mark.asyncio
async def test_restricted_postgres_backup_roundtrip_preserves_admin_extension(tmp_path):
    """Real pg_dump/restore, exclusively in a disposable database and role."""
    if os.environ.get("PASTORAL_TEST_POSTGRES") != "1":
        pytest.skip("Set PASTORAL_TEST_POSTGRES=1 to run isolated PostgreSQL integration")
    import asyncpg
    from sqlalchemy import insert, select, text, update
    from sqlalchemy.engine import make_url
    from pastoral_bot.knowledge import Knowledge
    from pastoral_bot.storage import Store
    from pastoral_bot.types import Mode
    from settings import settings as personal_settings

    admin_url = make_url(personal_settings.DATABASE_URL)
    suffix = uuid4().hex
    dbname = "pastoral_test_backup_" + suffix
    role = "pastoral_test_role_" + suffix
    password = uuid4().hex
    connection = dict(user=admin_url.username, password=admin_url.password,
                      host=admin_url.host, port=admin_url.port or 5432)
    admin = await asyncpg.connect(**connection, database="postgres")
    store = None
    database_created = role_created = False
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\' NOSUPERUSER NOCREATEDB NOCREATEROLE')
        role_created = True
        await admin.execute(f'CREATE DATABASE "{dbname}" OWNER "{role}"')
        database_created = True
        provision = await asyncpg.connect(**connection, database=dbname)
        try:
            await provision.execute("CREATE EXTENSION vector")
            # This administrator-owned object is not part of a bot snapshot.
            await provision.execute("CREATE TABLE public.unrelated_admin_data (value text)")
            await provision.execute("INSERT INTO public.unrelated_admin_data VALUES ('preserved')")
            extension_oid = await provision.fetchval("SELECT oid FROM pg_extension WHERE extname='vector'")
        finally:
            await provision.close()

        runtime_url = admin_url.set(database=dbname, username=role, password=password).render_as_string(hide_password=False)
        configuration = BotSettings(database_url=runtime_url, state_dir=tmp_path,
                                    backup_key=Fernet.generate_key().decode())
        journal = tmp_path / "journals" / "deletions.jsonl"
        modes = tmp_path / "journals" / "modes.jsonl"
        journal.parent.mkdir()
        journal.touch()
        modes.touch()
        store = Store(runtime_url, journal, modes)
        await store.initialize()
        knowledge = Knowledge(store)
        await knowledge.initialize()
        marker = "PRIVATE_BACKUP_ROUNDTRIP_СЕКРЕТ"
        old = await store.accept_message(1, 1, 1, marker)
        await store.complete(old, "ordinary answer", [1.0] + [0.0] * 383)
        await store.get_user(2)
        async with store.engine.begin() as conn:
            await conn.execute(insert(knowledge.documents).values(
                id="test-document", source_key="test", title="Test", edition="Test edition",
                canonical_url="https://example.org/source", version="v1", content_hash="0" * 64, approved=True,
            ))
            await conn.execute(insert(knowledge.passages).values(
                id="test-passage", document_id="test-document", ordinal=0, locator="1",
                url="https://example.org/source", text="Original corpus passage", embedding=[1.0] + [0.0] * 383,
            ))
        snapshot = await create_backup(configuration)
        await store.delete_history(1)
        await store.set_mode(2, Mode.CONFESSION)
        async with store.engine.begin() as conn:
            await conn.execute(update(knowledge.passages).values(text="Changed after backup"))

        await restore_backup(configuration, snapshot, store)

        assert await store.history(1, old.conversation_id) == []
        assert (await store.get_user(2)).mode == Mode.CONFESSION
        async with store.engine.connect() as conn:
            assert await conn.scalar(select(knowledge.passages.c.text)) == "Original corpus passage"
            assert await conn.scalar(text("SELECT oid FROM pg_extension WHERE extname='vector'")) == extension_oid
            assert await conn.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname='ix_pastoral_passages_russian'")) == 1
        # Owned sequences were included and can still allocate new messages.
        next_job = await store.accept_message(2, 1, 1, "new ordinary message")
        assert next_job.message_id > old.message_id
        assert await store.complete(next_job, "new ordinary answer", [1.0] + [0.0] * 383)
        probe = await asyncpg.connect(**connection, database=dbname)
        try:
            assert await probe.fetchval("SELECT value FROM public.unrelated_admin_data") == "preserved"
        finally:
            await probe.close()
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert marker.encode() not in path.read_bytes()
    finally:
        if store is not None:
            await store.close()
        # Names are generated locally, never a configured or user database.
        if database_created:
            await admin.execute(f'DROP DATABASE "{dbname}"')
        if role_created:
            await admin.execute(f'DROP ROLE "{role}"')
        await admin.close()


def test_retention_expires_encrypted_backups_even_when_backup_creation_fails(tmp_path):
    now = datetime.now(timezone.utc)
    old = tmp_path / "pastoral-old.dump.enc"
    recent = tmp_path / "pastoral-new.dump.enc"
    journal = tmp_path / "deletions.jsonl"
    for path in (old, recent, journal):
        path.touch()
    expired = (now - timedelta(days=8)).timestamp()
    os.utime(old, (expired, expired))
    prune(tmp_path, 7, now)
    assert not old.exists()
    assert recent.exists() and journal.exists()
