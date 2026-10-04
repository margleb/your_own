from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, update

from pastoral_bot.app import BotApp
from pastoral_bot.budget import Budget
from pastoral_bot.config import BotSettings
from pastoral_bot.storage import Store, jobs, messages, metadata, receipts
from pastoral_bot.turn import TurnError
from pastoral_bot.types import Mode, SourcePassage, TurnReply
from pastoral_bot.web import AuthenticationError, create_web_app, validate_init_data

TOKEN = "12345:test-token"
AUTH_NOW = int(time.time())


def signed(user_id=11, *, auth_date=None, extra=None, user_json=None):
    data = {"auth_date": str(int(time.time()) if auth_date is None else auth_date),
            "user": user_json or json.dumps({"id": user_id}), "query_id": "test-query"}
    data.update(extra or {})
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def headers(user_id=11, **kwargs):
    return {"Authorization": "tma " + signed(user_id, **kwargs)}


@pytest.mark.parametrize("extra", [{}, {"signature": "signed-third-party-value"}])
def test_authentication_validates_official_hmac_including_signature(extra):
    assert validate_init_data(signed(extra=extra), TOKEN) == 11


@pytest.mark.parametrize("raw", [
    signed().replace("test-query", "forged-query"),
    signed() + "&user=%7B%22id%22%3A22%7D",
    signed() + "&auth_date=123",
    signed() + "&bad=%XZ",
    signed(user_json='{"id":11,"id":22}'),
    signed(user_json='{"id":true}'),
    signed(user_id=-11), signed(user_id=0),
    signed(user_json='{"id":11,"is_bot":true}'),
    signed(auth_date=AUTH_NOW - 7201),
    signed(auth_date=AUTH_NOW + 61),
    signed(extra={"chat_type": "group"}),
    signed(extra={"chat_type": "supergroup"}),
    signed(extra={"chat_type": "channel"}),
    signed(extra={"chat": '{"id":-100,"type":"group"}'}),
])
def test_authentication_rejects_tampering_ambiguous_or_expired_payload(raw):
    with pytest.raises(AuthenticationError, match="invalid_telegram_auth"):
        validate_init_data(raw, TOKEN, now=AUTH_NOW)


@pytest.mark.parametrize("auth_date", [AUTH_NOW - 7200, AUTH_NOW + 60])
def test_authentication_accepts_exact_time_boundaries(auth_date):
    assert validate_init_data(signed(auth_date=auth_date), TOKEN, now=AUTH_NOW) == 11


@pytest.mark.parametrize("chat_type", ["sender", "private"])
def test_authentication_accepts_private_launch(chat_type):
    assert validate_init_data(signed(extra={"chat_type": chat_type}), TOKEN) == 11


@pytest.mark.parametrize("url", ["http://example.org/", "https://user:pass@example.org/", "https://example.org/app", "https://example.org/?x=1", "https://example.org/#x"])
def test_web_enabled_requires_safe_absolute_https_url(url):
    with pytest.raises(ValueError, match="HTTPS"):
        BotSettings(web_enabled=True, web_public_url=url)


def test_web_privacy_guardrail_does_not_claim_miniapp_text_is_in_telegram_chat():
    from pastoral_bot.guardrails import service_reply
    reply = service_reply("Где хранится наша переписка?", Mode.CONFESSION, BotSettings(), channel="web")
    assert "Сообщения из Mini App не отправляются в чат Telegram" in reply.text
    assert "Telegram сохраняет облачную переписку;" not in reply.text


class Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    async def send_chat_action(self, chat_id):
        pass


class Turn:
    def __init__(self, store, settings):
        self.budget = Budget(store, settings)
        self.knowledge = SimpleNamespace(embed=self.embed)
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, text):
        return None

    async def run(self, job, temporary_history=None):
        reservation = await self.budget.reserve(job.user_id, Decimal("0.01"))
        if reservation is None:
            raise TurnError("limits_reached")
        answered = False
        try:
            self.calls.append(job)
            if job.text.startswith("wait"):
                self.entered.set()
                await self.release.wait()
            if job.text.startswith("failure"):
                raise RuntimeError(job.text)
            source = SourcePassage("test-source:1", "Текст", "Редакция", "§ 1", "https://example.org/book", "Фрагмент")
            answered = True
            return TurnReply("answer:" + job.text + "\nИсточники...", [source.source_id], None,
                             body="answer:" + job.text, sources=[source])
        finally:
            await self.budget.settle(reservation, Decimal("0.001"), answered)


@pytest_asyncio.fixture
async def running(tmp_path):
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    await store.initialize()
    settings = BotSettings(telegram_token=TOKEN, daily_answer_limit=3)
    bot = BotApp(settings, store, Turn(store, settings), Client())
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<html>APP</html>")
    (tmp_path / "outside.txt").write_text("SECRET")
    client = TestClient(TestServer(create_web_app(bot, settings, static)))
    await client.start_server()
    yield client, bot
    await client.close()
    await bot.close()
    await store.close()


async def idle(bot):
    async def wait():
        while bot.workers:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 3)


async def post(client, path, payload, user_id=11):
    response = await client.post("/api/" + path, json=payload, headers=headers(user_id))
    return response, await response.json()


async def state(client, user_id=11):
    return await (await client.get("/api/state", headers=headers(user_id))).json()


async def submit(client, text, *, user_id=11, request_id=None, epoch=0):
    request_id = request_id or str(uuid4())
    response, body = await post(client, "messages", {"request_id": request_id, "text": text, "expected_epoch": epoch}, user_id)
    return request_id, response, body


@pytest.mark.asyncio
async def test_api_auth_origin_body_limits_and_static_security(running):
    client, bot = running
    response = await client.get("/api/state")
    assert response.status == 401
    assert response.headers["Cache-Control"] == "no-store"
    forged = headers()
    forged["Origin"] = "https://malicious.example"
    assert (await client.get("/api/state", headers=forged)).status == 403
    assert (await client.post("/api/messages", data="x" * 33000, headers={**headers(), "Content-Type": "application/json"})).status == 413
    assert (await client.get("/api/state?initData=" + signed())).status == 401
    page = await client.get("/")
    assert page.status == 200 and "APP" in await page.text()
    assert "https://telegram.org" in page.headers["Content-Security-Policy"]
    assert not page.headers.get("Access-Control-Allow-Origin")
    assert (await client.get("/%2e%2e/outside.txt")).status == 404
    assert (await client.get("/healthz")).status == 200
    assert not bot.turn.calls


@pytest.mark.asyncio
async def test_web_replies_are_owned_deduplicated_and_never_sent_to_telegram(running):
    client, bot = running
    request_id, response, _ = await submit(client, "alice")
    assert response.status == 202
    await idle(bot)
    _, duplicate, result = await submit(client, "forged replacement", request_id=request_id)
    assert duplicate.status == 202 and result["status"] == "done"
    assert result["reply"]["text"] == "answer:alice"
    assert result["reply"]["sources"][0]["text"] == "Фрагмент"
    чужой = await (await client.get(f"/api/messages/{request_id}", headers=headers(22))).json()
    assert чужой["status"] == "expired" and "reply" not in чужой
    await submit(client, "bob", user_id=22, request_id=request_id)
    await idle(bot)
    assert len(bot.turn.calls) == 2
    assert "alice" not in repr(await state(client, 22))
    assert "bob" not in repr(await state(client, 11))
    assert not bot.client.sent
    assert await bot.store.next_offset() is None


@pytest.mark.asyncio
async def test_telegram_and_web_share_fifo_quota_and_context(running):
    client, bot = running
    await submit(client, "wait first")
    await asyncio.wait_for(bot.turn.entered.wait(), 1)
    await bot.handle({"update_id": 12, "message": {"from": {"id": 11}, "chat": {"id": 11, "type": "private"}, "text": "telegram second"}})
    await submit(client, "web third")
    bot.turn.release.set()
    await idle(bot)
    assert [job.text for job in bot.turn.calls] == ["wait first", "telegram second", "web third"]
    assert len(bot.client.sent) == 1 and bot.client.sent[0][1].startswith("answer:telegram second")
    assert (await state(client))["remaining_answers"] == 0
    request_id, _, _ = await submit(client, "fourth")
    await idle(bot)
    result = await (await client.get(f"/api/messages/{request_id}", headers=headers())).json()
    assert result["status"] == "error" and result["error"]["code"] == "limits_reached"
    assert await bot.store.next_offset() == 13


@pytest.mark.asyncio
async def test_temporary_text_result_only_ram_and_polling_does_not_extend_lifetime(running, caplog):
    client, bot = running
    _, selected = await post(client, "mode", {"mode": "confession", "expected_epoch": 0})
    assert selected["temporary"]["active"]
    marker = "UNIQUE_WEB_CONFESSION_СЕКРЕТ"
    request_id, _, _ = await submit(client, marker, epoch=1)
    await idle(bot)
    assert marker in repr(await state(client))
    async with bot.store.engine.connect() as conn:
        for table in metadata.sorted_tables:
            assert marker not in repr((await conn.execute(select(table))).all())
    assert marker not in caplog.text
    session = bot.temporary.sessions[11]
    touched = session.touched
    await state(client)
    await client.get(f"/api/messages/{request_id}", headers=headers())
    assert session.touched == touched
    bot.temporary.clock = lambda: touched + bot.settings.temporary_idle_seconds + 1
    expired = await state(client)
    assert expired["mode"] == "confession" and expired["temporary"]["expired"] and expired["history"] == []
    assert not bot.web_requests
    _, rejected, data = await submit(client, "late", epoch=1)
    assert rejected.status == 409 and data["error"]["code"] == "temporary_expired"
    _, restarted = await post(client, "new", {"expected_epoch": 1})
    assert restarted["temporary"]["active"]
    assert not bot.client.sent


@pytest.mark.asyncio
async def test_temporary_failure_logs_only_error_class_and_restart_preserves_mode(running, caplog):
    client, bot = running
    await post(client, "mode", {"mode": "confession", "expected_epoch": 0})
    marker = "failure SECRET_IN_PROVIDER_EXCEPTION"
    await submit(client, marker, epoch=1)
    await idle(bot)
    assert "RuntimeError" in caplog.text and marker not in caplog.text
    restarted = BotApp(bot.settings, bot.store, bot.turn, bot.client)
    try:
        recovered = await restarted.web_state(11)
        assert recovered["mode"] == "confession" and recovered["temporary"]["expired"]
        assert recovered["history"] == []
        result, status = await restarted.web_submit(11, str(uuid4()), "new private text", 1)
        assert status == 409 and result["code"] == "temporary_expired"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_delete_cancels_work_and_stale_epoch_does_not_restore_history(running):
    client, bot = running
    request_id, _, _ = await submit(client, "wait late private")
    await asyncio.wait_for(bot.turn.entered.wait(), 1)
    response, deleted = await post(client, "delete-history", {"expected_epoch": 0, "confirm": True})
    assert response.status == 200 and deleted["history"] == [] and deleted["epoch"] == 1
    bot.turn.release.set()
    await idle(bot)
    assert (await state(client))["history"] == []
    result = await (await client.get(f"/api/messages/{request_id}", headers=headers())).json()
    assert result["status"] == "expired" and "reply" not in result
    _, stale, body = await submit(client, "old tab", epoch=0)
    assert stale.status == 409 and body["error"]["code"] == "stale_epoch"
    no_confirmation, _ = await post(client, "delete-history", {"expected_epoch": 1, "confirm": False})
    assert no_confirmation.status == 400


@pytest.mark.asyncio
async def test_ordinary_web_jobs_recover_without_telegram_delivery(running):
    _, bot = running
    request_id = str(uuid4())
    job = await bot.store.accept_message(bot.web_update_id(11, request_id), 11, 11, "recover question", channel="web", request_id=request_id)
    await bot.recover()
    await idle(bot)
    assert (await bot.web_result(11, request_id))["status"] == "done"
    assert not bot.client.sent
    assert not await bot.store.pending_deliveries()
    next_id = str(uuid4())
    job = await bot.store.accept_message(bot.web_update_id(11, next_id), 11, 11, "generated question", channel="web", request_id=next_id)
    await bot.store.complete(job, "safe answer", reply_metadata={"text": "safe answer", "sources": [], "referral": None})
    await bot.recover()
    assert (await bot.web_result(11, next_id))["reply"]["text"] == "safe answer"
    assert not bot.client.sent


@pytest.mark.asyncio
async def test_recovery_never_enqueues_same_inflight_web_request_twice(running):
    client, bot = running
    await submit(client, "wait one request")
    await asyncio.wait_for(bot.turn.entered.wait(), 1)
    await bot.recover()
    bot.turn.release.set()
    await idle(bot)
    assert len(bot.turn.calls) == 1


@pytest.mark.asyncio
async def test_stop_cancels_confession_while_waiting_for_global_capacity(running):
    client, bot = running
    bot.semaphore = asyncio.Semaphore(0)
    await post(client, "mode", {"mode": "confession", "expected_epoch": 0})
    await submit(client, "WAITING_PRIVATE_TEXT", epoch=1)
    async def acquired_waiter():
        while 11 not in bot.acquiring:
            await asyncio.sleep(0)
    await asyncio.wait_for(acquired_waiter(), 1)
    response, stopped = await post(client, "stop", {"expected_epoch": 1})
    assert response.status == 200 and stopped["temporary"]["expired"]
    await idle(bot)
    assert not bot.acquiring and not bot.queues and not bot.queued_ids
    assert not bot.web_requests and not bot.temporary.sessions and not bot.turn.calls
    assert not bot.client.sent


@pytest.mark.asyncio
async def test_rolling_temporary_history_keeps_stable_nonreused_ram_ids(running):
    client, bot = running
    await post(client, "mode", {"mode": "confession", "expected_epoch": 0})
    for index in range(8):
        bot.temporary.append(11, 1, f"question {index}", f"answer {index}")
    before = (await state(client))["history"]
    assert len(before) == 16
    original_ids = {item["id"] for item in before}
    bot.temporary.append(11, 1, "question 8", "answer 8", reply_metadata={"text": "clean answer", "sources": []})
    after = (await state(client))["history"]
    assert len(after) == 16
    assert after[:-2] == before[2:]
    assert not original_ids.intersection(item["id"] for item in after[-2:])
    assert len({item["id"] for item in after}) == 16
    model_context, fresh = bot.temporary.context(11, 1)
    assert not fresh and all(set(item) == {"role", "content"} for item in model_context)
    assert model_context[-1] == {"role": "assistant", "content": "answer 8"}
    bot.temporary.start(11, 1)  # a fresh session, even when an epoch is reused
    bot.temporary.append(11, 1, "new question", "new answer")
    restarted_ids = {item["id"] for item in (await state(client))["history"]}
    assert not restarted_ids.intersection(original_ids | {item["id"] for item in after})


@pytest.mark.asyncio
async def test_legacy_rollback_cancels_web_queue_and_receipts_preserving_telegram(running):
    """Old images have no channel filter; cancelled web rows must be invisible.

    Simulate the operator's atomic PostgreSQL cleanup in SQLite. The actual
    runbook additionally holds advisory lock 7261032026 after stopping the bot.
    """
    _, bot = running
    created = {}
    for channel in ("web", "telegram"):
        for index, status in enumerate(("pending", "running", "generated", "done")):
            request_id = str(uuid4()) if channel == "web" else None
            update_id = bot.web_update_id(11, request_id) if request_id else 500 + index
            job = await bot.store.accept_message(update_id, 11, 11, f"{channel} {status}", channel=channel, request_id=request_id)
            if status in ("generated", "done"):
                assert await bot.store.complete(job, f"answer {channel} {status}")
            if status in ("running", "done"):
                await bot.store.finish_job(update_id, status)
            created[channel, status] = update_id
    async with bot.store.engine.begin() as conn:
        pending_web = list((await conn.execute(select(jobs.c.update_id).where(
            jobs.c.channel == "web", jobs.c.status.in_(["pending", "running", "generated"]),
        ))).scalars())
        await conn.execute(update(jobs).where(jobs.c.update_id.in_(pending_web)).values(status="cancelled", error_code="legacy_rollback"))
        await conn.execute(update(receipts).where(receipts.c.update_id.in_(pending_web)).values(status="cancelled"))
    async with bot.store.engine.connect() as conn:
        # Exactly the status selectors used by the pre-Mini-App Store, without
        # filtering jobs.channel. No web request may resume through Telegram.
        old_pending = set((await conn.execute(select(jobs.c.update_id).where(jobs.c.status.in_(["pending", "running"])))).scalars())
        outgoing = messages.alias("old_outgoing")
        old_deliveries = set((await conn.execute(select(jobs.c.update_id).join(outgoing, outgoing.c.reply_to_id == jobs.c.message_id).where(
            jobs.c.status == "generated", outgoing.c.user_id == jobs.c.user_id,
        ))).scalars())
        assert old_pending == {created["telegram", "pending"], created["telegram", "running"]}
        assert old_deliveries == {created["telegram", "generated"]}
        for status in ("pending", "running", "generated"):
            assert await conn.scalar(select(receipts.c.status).where(receipts.c.update_id == created["web", status])) == "cancelled"
            assert await conn.scalar(select(jobs.c.status).where(jobs.c.update_id == created["telegram", status])) == status
        assert await conn.scalar(select(jobs.c.status).where(jobs.c.update_id == created["web", "done"])) == "done"
