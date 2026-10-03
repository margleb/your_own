"""Isolated persistence for the public bot. Confession payloads never enter SQL."""
from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, JSON, MetaData,
    Numeric, String, Table, Text, delete, func, insert, select,
    text as sql_text, update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.types import UserDefinedType

from .types import Mode, TurnJob, UserState


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Vector384(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw):
        return "VECTOR(384)"

    def bind_processor(self, dialect):
        return lambda value: None if value is None else json.dumps(list(value))

    def result_processor(self, dialect, coltype):
        return lambda value: json.loads(value) if isinstance(value, str) else value


def vector_type():
    return JSON().with_variant(Vector384(), "postgresql")


metadata = MetaData()
users = Table(
    "pastoral_users", metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("mode", String(16), nullable=False),
    Column("conversation_id", String(36), nullable=False),
    Column("epoch", Integer, nullable=False, default=0),
    Column("campaign", String(64)),
    Column("created_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime, nullable=False),
    Column("first_answer_at", DateTime),
    Column("last_answer_at", DateTime),
    Column("returned", Boolean, nullable=False, default=False),
)
conversations = Table(
    "pastoral_conversations", metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", BigInteger, ForeignKey(users.c.user_id, ondelete="CASCADE"), nullable=False, index=True),
    Column("mode", String(16), nullable=False),
    Column("created_at", DateTime, nullable=False),
)
messages = Table(
    "pastoral_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, ForeignKey(users.c.user_id, ondelete="CASCADE"), nullable=False, index=True),
    Column("conversation_id", String(36), ForeignKey(conversations.c.id, ondelete="CASCADE"), nullable=False, index=True),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("reply_to_id", Integer, ForeignKey("pastoral_messages.id", ondelete="CASCADE")),
    Column("created_at", DateTime, nullable=False),
)
chunks = Table(
    "pastoral_dialogue_chunks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, ForeignKey(users.c.user_id, ondelete="CASCADE"), nullable=False, index=True),
    Column("conversation_id", String(36), ForeignKey(conversations.c.id, ondelete="CASCADE"), nullable=False),
    Column("user_message_id", Integer, ForeignKey(messages.c.id, ondelete="CASCADE"), nullable=False),
    Column("assistant_message_id", Integer, ForeignKey(messages.c.id, ondelete="CASCADE"), nullable=False),
    Column("embedding", vector_type(), nullable=False),
)
receipts = Table(
    "pastoral_telegram_receipts", metadata,
    Column("update_id", BigInteger, primary_key=True, autoincrement=False),
    Column("status", String(24), nullable=False),
    Column("created_at", DateTime, nullable=False),
)
jobs = Table(
    "pastoral_jobs", metadata,
    Column("update_id", BigInteger, ForeignKey(receipts.c.update_id, ondelete="CASCADE"), primary_key=True),
    Column("user_id", BigInteger, ForeignKey(users.c.user_id, ondelete="CASCADE"), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("conversation_id", String(36), ForeignKey(conversations.c.id, ondelete="CASCADE"), nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("mode", String(16), nullable=False),
    Column("message_id", Integer, ForeignKey(messages.c.id, ondelete="CASCADE"), nullable=False),
    Column("status", String(24), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("delivered_chunks", Integer, nullable=False, default=0),
)
deletions = Table(
    "pastoral_deletion_events", metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("deleted_before", DateTime, nullable=False),
    Column("epoch", Integer, nullable=False),
)
budget_days = Table(
    "pastoral_budget_days", metadata,
    Column("day", String(10), primary_key=True),
    Column("spent", Numeric(18, 8), nullable=False),
    Column("reserved", Numeric(18, 8), nullable=False),
)
quotas = Table(
    "pastoral_daily_quotas", metadata,
    Column("day", String(10), primary_key=True),
    Column("user_id", BigInteger, primary_key=True),
    Column("answered", Integer, nullable=False),
    Column("pending", Integer, nullable=False),
)
reservations = Table(
    "pastoral_budget_reservations", metadata,
    Column("id", String(36), primary_key=True),
    Column("day", String(10), ForeignKey(budget_days.c.day), nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("amount", Numeric(18, 8), nullable=False),
    Column("actual", Numeric(18, 8)),
    Column("state", String(16), nullable=False),
    Column("answered", Boolean, nullable=False),
)


async def insert_once(conn, table, values, keys) -> bool:
    dialect = conn.dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=keys)
    elif dialect == "sqlite":
        stmt = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=keys)
    else:
        raise RuntimeError("Unsupported pastoral database dialect")
    return (await conn.execute(stmt)).rowcount == 1


class Store:
    def __init__(self, database_url: str | AsyncEngine, deletion_journal: str | Path | None = None, mode_journal: str | Path | None = None):
        self.engine = database_url if isinstance(database_url, AsyncEngine) else create_async_engine(database_url, hide_parameters=True)
        self.lock = asyncio.Lock()
        self.deletion_journal = Path(deletion_journal) if deletion_journal else None
        self.mode_journal = Path(mode_journal) if mode_journal else None

    async def initialize(self):
        async with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            elif conn.dialect.name == "sqlite":
                await conn.execute(sql_text("PRAGMA foreign_keys=ON"))
            await conn.run_sync(metadata.create_all)

    async def close(self):
        await self.engine.dispose()

    @staticmethod
    def _state(row) -> UserState:
        return UserState(user_id=row["user_id"], mode=Mode(row["mode"]), conversation_id=row["conversation_id"], epoch=row["epoch"])

    async def _user(self, conn, user_id: int):
        now = utcnow()
        conv = str(uuid4())
        created = await insert_once(conn, users, dict(user_id=user_id, mode=Mode.TALK.value, conversation_id=conv, epoch=0, created_at=now, last_seen_at=now), ["user_id"])
        if created:
            await conn.execute(insert(conversations).values(id=conv, user_id=user_id, mode=Mode.TALK.value, created_at=now))
        return (await conn.execute(select(users).where(users.c.user_id == user_id).with_for_update())).mappings().one()

    async def get_user(self, user_id: int) -> UserState:
        async with self.lock, self.engine.begin() as conn:
            return self._state(await self._user(conn, user_id))

    async def accept_control(self, update_id: int) -> bool:
        async with self.lock, self.engine.begin() as conn:
            return await insert_once(conn, receipts, dict(update_id=update_id, status="control", created_at=utcnow()), ["update_id"])

    async def accept_message(self, update_id: int, user_id: int, chat_id: int, text: str) -> TurnJob | None:
        async with self.lock, self.engine.begin() as conn:
            if not await insert_once(conn, receipts, dict(update_id=update_id, status="accepted", created_at=utcnow()), ["update_id"]):
                return None
            state = self._state(await self._user(conn, user_id))
            await conn.execute(update(users).where(users.c.user_id == user_id).values(last_seen_at=utcnow()))
            message_id = None
            if state.mode != Mode.CONFESSION:
                result = await conn.execute(insert(messages).values(user_id=user_id, conversation_id=state.conversation_id, role="user", content=text, created_at=utcnow()))
                message_id = result.inserted_primary_key[0]
                await conn.execute(insert(jobs).values(update_id=update_id, user_id=user_id, chat_id=chat_id, conversation_id=state.conversation_id, epoch=state.epoch, mode=state.mode.value, message_id=message_id, status="pending", created_at=utcnow(), delivered_chunks=0))
            return TurnJob(update_id=update_id, user_id=user_id, chat_id=chat_id, conversation_id=state.conversation_id, epoch=state.epoch, mode=state.mode, text=text, message_id=message_id)

    async def next_offset(self) -> int | None:
        async with self.engine.connect() as conn:
            value = await conn.scalar(select(func.max(receipts.c.update_id)))
            return value + 1 if value is not None else None

    async def pending_jobs(self) -> list[TurnJob]:
        async with self.engine.connect() as conn:
            query = select(jobs, messages.c.content).join(messages, jobs.c.message_id == messages.c.id).where(jobs.c.status.in_(["pending", "running"])).order_by(jobs.c.update_id)
            rows = (await conn.execute(query)).mappings().all()
            return [TurnJob(update_id=r["update_id"], user_id=r["user_id"], chat_id=r["chat_id"], conversation_id=r["conversation_id"], epoch=r["epoch"], mode=Mode(r["mode"]), text=r["content"], message_id=r["message_id"]) for r in rows]

    async def _change(self, conn, user_id: int, mode: Mode | None = None, new: bool = True) -> UserState:
        row = await self._user(conn, user_id)
        mode = mode or Mode(row["mode"])
        conv = str(uuid4()) if new else row["conversation_id"]
        if new and mode != Mode.CONFESSION:
            await conn.execute(insert(conversations).values(id=conv, user_id=user_id, mode=mode.value, created_at=utcnow()))
        epoch = row["epoch"] + 1
        await conn.execute(update(users).where(users.c.user_id == user_id).values(mode=mode.value, conversation_id=conv, epoch=epoch))
        await conn.execute(update(jobs).where(jobs.c.user_id == user_id, jobs.c.status.in_(["pending", "running", "generated"])).values(status="cancelled"))
        return UserState(user_id=user_id, mode=mode, conversation_id=conv, epoch=epoch)

    async def set_mode(self, user_id: int, mode: Mode) -> UserState:
        async with self.lock, self.engine.begin() as conn:
            await self._user(conn, user_id)
            await asyncio.to_thread(self._append_record, self.mode_journal, {"user_id": user_id, "mode": mode.value})
            return await self._change(conn, user_id, mode)

    async def transition_callback(self, update_id: int, user_id: int, expected_epoch: int, mode: Mode | None = None, delete_history: bool = False) -> UserState | None:
        """Commit callback receipt and control transition before any Telegram ACK."""
        if (mode is None) == (not delete_history):
            raise ValueError("Callback must specify exactly one transition")
        async with self.lock, self.engine.begin() as conn:
            if not await insert_once(conn, receipts, dict(update_id=update_id, status="control", created_at=utcnow()), ["update_id"]):
                return None
            row = await self._user(conn, user_id)
            if row["epoch"] != expected_epoch:
                return None
            if delete_history:
                return await self._delete_history(conn, user_id)
            await asyncio.to_thread(self._append_record, self.mode_journal, {"user_id": user_id, "mode": mode.value})
            return await self._change(conn, user_id, mode)

    async def new_conversation(self, user_id: int) -> UserState:
        async with self.lock, self.engine.begin() as conn:
            return await self._change(conn, user_id)

    async def stop(self, user_id: int) -> UserState:
        async with self.lock, self.engine.begin() as conn:
            return await self._change(conn, user_id, new=False)

    @staticmethod
    def _append_record(path, event):
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def delete_history(self, user_id: int) -> UserState:
        async with self.lock, self.engine.begin() as conn:
            return await self._delete_history(conn, user_id)

    async def _delete_history(self, conn, user_id):
        row = await self._user(conn, user_id)
        cutoff = utcnow()
        event = dict(id=str(uuid4()), user_id=user_id, deleted_before=cutoff.isoformat(), epoch=row["epoch"] + 1)
        # Journal is outside backup snapshots; fsync BEFORE deletion so a crash cannot
        # resurrect data on restore. Replaying a harmless premature deletion is safe.
        await asyncio.to_thread(self._append_record, self.deletion_journal, event)
        await conn.execute(insert(deletions).values(**{**event, "deleted_before": cutoff}))
        await conn.execute(delete(chunks).where(chunks.c.user_id == user_id))
        await conn.execute(delete(jobs).where(jobs.c.user_id == user_id))
        await conn.execute(delete(messages).where(messages.c.user_id == user_id))
        await conn.execute(delete(conversations).where(conversations.c.user_id == user_id))
        return await self._change(conn, user_id)

    async def replay_modes(self):
        """An old backup must never silently turn confession storage back on."""
        if self.mode_journal is None or not self.mode_journal.exists():
            return
        latest = {}
        for line in self.mode_journal.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                latest[int(record["user_id"])] = Mode(record["mode"])
        async with self.lock, self.engine.begin() as conn:
            for user_id, mode in latest.items():
                row = await self._user(conn, user_id)
                if row["mode"] != mode.value:
                    await self._change(conn, user_id, mode)

    async def replay_deletions(self):
        if self.deletion_journal is None or not self.deletion_journal.exists():
            return
        events = [json.loads(line) for line in self.deletion_journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        async with self.lock, self.engine.begin() as conn:
            for event in events:
                cutoff = datetime.fromisoformat(event["deleted_before"])
                user_id = int(event["user_id"])
                await insert_once(conn, deletions, {**event, "deleted_before": cutoff}, ["id"])
                old_ids = select(messages.c.id).where(messages.c.user_id == user_id, messages.c.created_at <= cutoff)
                await conn.execute(delete(chunks).where(chunks.c.user_id == user_id, chunks.c.user_message_id.in_(old_ids)))
                await conn.execute(delete(jobs).where(jobs.c.user_id == user_id, jobs.c.created_at <= cutoff))
                await conn.execute(delete(messages).where(messages.c.user_id == user_id, messages.c.created_at <= cutoff))
                row = (await conn.execute(select(users).where(users.c.user_id == user_id))).mappings().one_or_none()
                if row and row["epoch"] < event["epoch"]:
                    await self._change(conn, user_id)
                    await conn.execute(update(users).where(users.c.user_id == user_id).values(epoch=event["epoch"]))

    @staticmethod
    def _matches(row, job: TurnJob) -> bool:
        return bool(row and row["epoch"] == job.epoch and row["conversation_id"] == job.conversation_id and row["mode"] == job.mode.value)

    async def is_current(self, job: TurnJob) -> bool:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(users).where(users.c.user_id == job.user_id))).mappings().one_or_none()
            return self._matches(row, job)

    async def history(self, user_id: int, conversation_id: str) -> list[dict]:
        async with self.engine.connect() as conn:
            # Accepted/queued user messages have no reply yet and must not appear
            # in another queued turn's context or be appended twice by PastoralTurn.
            completed = (await conn.execute(select(messages.c.id, messages.c.reply_to_id).where(messages.c.user_id == user_id, messages.c.conversation_id == conversation_id, messages.c.role == "assistant").order_by(messages.c.id.desc()).limit(8))).mappings().all()
            ids = [value for row in completed for value in (row["reply_to_id"], row["id"]) if value is not None]
            if not ids:
                return []
            rows = (await conn.execute(select(messages.c.role, messages.c.content).where(messages.c.user_id == user_id, messages.c.conversation_id == conversation_id, messages.c.id.in_(ids)).order_by(messages.c.id))).mappings().all()
            return [dict(role=r["role"], content=r["content"]) for r in rows]

    async def recall(self, user_id: int, conversation_id: str, vector) -> list[dict]:
        if vector is None:
            return []
        async with self.engine.connect() as conn:
            own = select(chunks).where(chunks.c.user_id == user_id, chunks.c.conversation_id != conversation_id)
            if conn.dialect.name == "postgresql":
                own = own.order_by(chunks.c.embedding.op("<=>")(list(vector))).limit(3)
                found = (await conn.execute(own)).mappings().all()
            else:
                found = (await conn.execute(own)).mappings().all()
                def distance(row):
                    left = row["embedding"]
                    divisor = math.sqrt(sum(x*x for x in left) * sum(x*x for x in vector))
                    return 1 - sum(a*b for a, b in zip(left, vector)) / divisor if divisor else 1
                found = sorted(found, key=distance)[:3]
            result = []
            for chunk in found:
                pair = (await conn.execute(select(messages.c.role, messages.c.content).where(messages.c.user_id == user_id, messages.c.id.in_([chunk["user_message_id"], chunk["assistant_message_id"]])).order_by(messages.c.id))).mappings().all()
                result.extend(dict(role=r["role"], content=r["content"]) for r in pair)
            return result

    async def complete(self, job: TurnJob, text: str, vector=None) -> bool:
        async with self.lock, self.engine.begin() as conn:
            row = (await conn.execute(select(users).where(users.c.user_id == job.user_id).with_for_update())).mappings().one_or_none()
            if not self._matches(row, job):
                return False
            now = utcnow()
            if job.mode == Mode.CONFESSION:
                await self._mark_answer(conn, row, now)
                return True
            saved = (await conn.execute(select(jobs).where(jobs.c.update_id == job.update_id, jobs.c.user_id == job.user_id))).mappings().one_or_none()
            if not saved or saved["status"] not in ("pending", "running") or saved["epoch"] != job.epoch or saved["conversation_id"] != job.conversation_id:
                return False
            await self._mark_answer(conn, row, now)
            result = await conn.execute(insert(messages).values(user_id=job.user_id, conversation_id=job.conversation_id, role="assistant", content=text, reply_to_id=saved["message_id"], created_at=utcnow()))
            assistant_id = result.inserted_primary_key[0]
            if vector is not None:
                await conn.execute(insert(chunks).values(user_id=job.user_id, conversation_id=job.conversation_id, user_message_id=saved["message_id"], assistant_message_id=assistant_id, embedding=list(vector)))
            await conn.execute(update(jobs).where(jobs.c.update_id == job.update_id).values(status="generated"))
            return True

    @staticmethod
    async def _mark_answer(conn, row, now):
        await conn.execute(update(users).where(users.c.user_id == row["user_id"]).values(first_answer_at=row["first_answer_at"] or now, last_answer_at=now, returned=row["returned"] or bool(row["first_answer_at"] and now.date() > row["first_answer_at"].date())))

    async def finish_job(self, update_id: int, status: str):
        async with self.lock, self.engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.update_id == update_id).values(status=status))
            await conn.execute(update(receipts).where(receipts.c.update_id == update_id).values(status=status))

    async def record_delivery(self, update_id: int, delivered_chunks: int):
        async with self.lock, self.engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.update_id == update_id).values(delivered_chunks=delivered_chunks))

    async def delivery_state(self, update_id: int) -> int:
        async with self.engine.connect() as conn:
            return (await conn.scalar(select(jobs.c.delivered_chunks).where(jobs.c.update_id == update_id))) or 0

    async def mark_delivered(self, update_id: int, delivered_count: int):
        """Record the cumulative number of successfully sent chunks."""
        async with self.lock, self.engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.update_id == update_id, jobs.c.delivered_chunks < delivered_count).values(delivered_chunks=delivered_count))

    async def pending_deliveries(self) -> list[tuple[TurnJob, str, int]]:
        """Ordinary already-generated replies resume delivery without another LLM call."""
        incoming = messages.alias("incoming")
        outgoing = messages.alias("outgoing")
        async with self.engine.connect() as conn:
            query = select(jobs, incoming.c.content.label("incoming_text"), outgoing.c.content.label("reply_text")).join(incoming, jobs.c.message_id == incoming.c.id).join(outgoing, outgoing.c.reply_to_id == incoming.c.id).where(jobs.c.status == "generated", outgoing.c.user_id == jobs.c.user_id).order_by(jobs.c.update_id)
            rows = (await conn.execute(query)).mappings().all()
            return [(TurnJob(update_id=r["update_id"], user_id=r["user_id"], chat_id=r["chat_id"], conversation_id=r["conversation_id"], epoch=r["epoch"], mode=Mode(r["mode"]), text=r["incoming_text"], message_id=r["message_id"]), r["reply_text"], r["delivered_chunks"]) for r in rows]

    async def attribute(self, user_id: int, campaign: str):
        if not campaign or len(campaign) > 64 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in campaign):
            return
        async with self.lock, self.engine.begin() as conn:
            await self._user(conn, user_id)
            await conn.execute(update(users).where(users.c.user_id == user_id, users.c.campaign.is_(None)).values(campaign=campaign))

    async def metrics(self) -> list[dict]:
        async with self.engine.connect() as conn:
            from sqlalchemy import case
            query = select(users.c.campaign, func.count(users.c.user_id).label("starts"), func.count(users.c.first_answer_at).label("first_answers"), func.sum(case((users.c.returned, 1), else_=0)).label("returned_users")).group_by(users.c.campaign)
            return [dict(row) for row in (await conn.execute(query)).mappings()]
