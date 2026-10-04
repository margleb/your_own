"""Private Telegram dispatcher with per-user ordering and cancellation epochs."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
from weakref import WeakValueDictionary

from infrastructure.telegram.client import TelegramError
from pastoral_bot.temporary import TemporarySessions
from pastoral_bot.telegram import Sender, split_message
from pastoral_bot.types import Mode, TurnJob, UserState

logger = logging.getLogger("pastoral.runtime")

INTRO = (
    "Я — «Православный помощник», ИИ и независимый проект. Можно поговорить, задать вопрос о вере "
    "или подготовиться к разговору со священником. Я не принимаю исповедь и не отпускаю грехи.\n\n"
    "Обычные разговоры сохраняются до /delete_history. В подготовке к исповеди текст хранится только "
    "в оперативной памяти нашего сервиса. Telegram сохраняет вашу переписку; сообщения также передаются "
    "модели через OpenRouter с маршрутизацией Zero Data Retention.\n\n"
    "Начните с выбора режима."
)
PRIVACY = (
    "Обычная история сохраняется в отдельной базе до /delete_history и используется только для ваших ответов. "
    "Мы не создаём психологические профили и не пересылаем беседы другим людям.\n\n"
    "В подготовке к исповеди текст не записывается в базу, индекс, долговечную очередь или журналы. "
    "Контекст исчезает после 30 минут бездействия, через 2 часа или при перезапуске. Режим остаётся временным "
    "до вашего явного переключения.\n\n"
    "Telegram хранит облачную переписку. Тексты передаются модели через OpenRouter к провайдерам с политикой "
    "Zero Data Retention. Это не таинство и не обещание полной анонимности.\n\n"
    "Резервные копии обычной истории зашифрованы и хранятся до 7 дней. Удаление применяется сразу в рабочей "
    "базе и повторяется при восстановлении. Удаление в боте не удаляет сообщения из Telegram."
)
HELP = (
    "/mode — выбрать режим\n/new — новая тема (обычная память остаётся)\n/stop — остановить ответ\n"
    "/privacy — хранение данных\n/delete_history — удалить сохранённую историю\n\n"
    "До 10 ответов в сутки; сутки считаются по московскому времени. "
    "Бот принимает текст в личном чате. Подготовка к исповеди помогает составить заметку: "
    "попросите подвести итог, затем уточните или исправьте его в беседе."
)
TEMP_NOTICE = (
    "Вы в режиме подготовки к исповеди. Временная беседа началась с чистого контекста: "
    "предыдущий контекст мог исчезнуть после перерыва или перезапуска. "
    "Текст не сохраняется в нашем сервисе; в Telegram переписка остаётся."
)
MODE_LABELS = {Mode.TALK: "Поговорить", Mode.FAITH: "Узнать о вере", Mode.CONFESSION: "Подготовиться к исповеди"}


def duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч"
    if seconds % 60 == 0:
        return f"{seconds // 60} мин"
    return f"{seconds} с"


def mode_keyboard(state: UserState) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": f"mode:{state.epoch}:{mode.value}"}]
                                for mode, label in MODE_LABELS.items()]}


class BotApp:
    def __init__(self, settings, store, turn, client):
        self.settings = settings
        self.store = store
        self.turn = turn
        self.client = client
        self.sender = Sender(client)
        self.temporary = TemporarySessions(settings.temporary_idle_seconds, settings.temporary_max_seconds)
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.queues: dict[int, asyncio.Queue] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.active: dict[int, asyncio.Task] = {}
        self.acquiring: dict[int, asyncio.Task] = {}
        self.arrivals: dict[int, float] = {}
        self.queued_ids: set[int] = set()
        self.user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        # Only confession results live here; ordinary replies are owned rows in SQL.
        self.web_requests: dict[tuple[int, str], dict] = {}
        self.closed = False

    def user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self.user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self.user_locks[user_id] = lock
        return lock

    async def handle(self, update: dict) -> None:
        callback = update.get("callback_query") or {}
        message = callback.get("message") if callback else update.get("message")
        author = callback.get("from") if callback else (message or {}).get("from")
        user_id = (author or {}).get("id")
        chat = (message or {}).get("chat") or {}
        if type(user_id) is int and user_id > 0 and chat.get("type") == "private" and chat.get("id") == user_id:
            async with self.user_lock(user_id):
                await self._handle_update(update)
        else:
            await self._handle_update(update)

    async def _handle_update(self, update: dict) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        callback = update.get("callback_query")
        message = (callback or {}).get("message") if callback else update.get("message")
        author = (callback or {}).get("from") if callback else (message or {}).get("from")
        chat = (message or {}).get("chat") or {}
        user_id = (author or {}).get("id")
        if chat.get("type") != "private" or not isinstance(user_id, int) or chat.get("id") != user_id or (author or {}).get("is_bot"):
            await self.store.accept_control(update_id)
            if callback:
                with suppress(TelegramError):
                    await self.client.answer_callback_query(callback["id"], "Доступно в личном чате")
            return
        if callback:
            await self._callback(update_id, user_id, callback)
            return
        text = (message or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            if await self.store.accept_control(update_id):
                await self.sender.text(user_id, "Пожалуйста, отправьте текстовое сообщение.")
            return
        if text.startswith("/"):
            if await self.store.accept_control(update_id):
                await self._command(user_id, text)
            return
        if len(text) > self.settings.max_message_chars:
            if await self.store.accept_control(update_id):
                await self.sender.text(user_id, f"Разделите сообщение на части до {self.settings.max_message_chars} символов.")
            return
        if sum(q.qsize() for q in self.queues.values()) + len(self.workers) >= self.settings.max_pending_jobs or (self.queues.get(user_id) and self.queues[user_id].qsize() >= 8):
            if await self.store.accept_control(update_id):
                await self.sender.text(user_id, "Уже есть несколько сообщений в очереди. Дождитесь ответа или используйте /stop.")
            return
        job = await self.store.accept_message(update_id, user_id, user_id, text)
        if job:
            self.enqueue(job)

    async def _command(self, user_id: int, text: str) -> None:
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command == "/start":
            campaign = argument.strip()
            if campaign in self.settings.campaign_ids and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", campaign):
                await self.store.attribute(user_id, campaign)
            state = await self.store.get_user(user_id)
            keyboard = mode_keyboard(state)
            if self.settings.web_enabled:
                keyboard["inline_keyboard"].insert(0, [{"text": "Открыть приложение", "web_app": {"url": self.settings.web_public_url}}])
            await self.sender.text(user_id, INTRO, keyboard=keyboard)
        elif command in ("/mode", "/help"):
            state = await self.store.get_user(user_id)
            await self.sender.text(user_id, HELP.replace("До 10 ответов", f"До {self.settings.daily_answer_limit} ответов") if command == "/help" else "Выберите режим:", keyboard=mode_keyboard(state))
        elif command == "/privacy":
            privacy = PRIVACY.replace("30 минут", duration(self.settings.temporary_idle_seconds))
            privacy = privacy.replace("2 часа", duration(self.settings.temporary_max_seconds))
            privacy = privacy.replace("7 дней", f"{self.settings.backup_retention_days} суток")
            await self.sender.text(user_id, privacy)
        elif command in ("/stop", "/new"):
            state = await (self.store.stop(user_id) if command == "/stop" else self.store.new_conversation(user_id))
            await self._cancel(user_id)
            if command == "/new" and state.mode == Mode.CONFESSION:
                self.temporary.start(user_id, state.epoch)
            await self.sender.text(user_id, "Ответ остановлен." if command == "/stop" else "Началась новая тема.", keyboard=mode_keyboard(state))
        elif command == "/delete_history":
            state = await self.store.get_user(user_id)
            keyboard = {"inline_keyboard": [[{"text": "Удалить историю", "callback_data": f"delete:{state.epoch}:yes"},
                                               {"text": "Отмена", "callback_data": f"delete:{state.epoch}:no"}]]}
            await self.sender.text(user_id, "Удалить всю сохранённую историю и поисковые фрагменты?", keyboard=keyboard)
        else:
            await self.sender.text(user_id, HELP.replace("До 10 ответов", f"До {self.settings.daily_answer_limit} ответов"))

    async def _callback(self, update_id: int, user_id: int, callback: dict) -> None:
        fields = str(callback.get("data", "")).split(":")
        state = await self.store.get_user(user_id)
        if len(fields) != 3 or not fields[1].isdigit() or int(fields[1]) != state.epoch:
            await self.store.accept_control(update_id)
            await self.client.answer_callback_query(callback["id"], "Кнопка устарела. Используйте /mode")
            return
        action, _, value = fields
        if action == "mode" and value in {m.value for m in Mode}:
            state = await self.store.transition_callback(update_id, user_id, int(fields[1]), mode=Mode(value))
            if state is None:
                await self.client.answer_callback_query(callback["id"])
                return
            await self._cancel(user_id)
            if state.mode == Mode.CONFESSION:
                self.temporary.start(user_id, state.epoch)
            await self.client.answer_callback_query(callback["id"])
            text = TEMP_NOTICE if state.mode == Mode.CONFESSION else f"Режим: {MODE_LABELS[state.mode]}. Обычная история сохраняется."
            await self.sender.text(user_id, text, keyboard=mode_keyboard(state))
        elif action == "delete" and value == "yes":
            state = await self.store.transition_callback(update_id, user_id, int(fields[1]), delete_history=True)
            if state is None:
                await self.client.answer_callback_query(callback["id"])
                return
            await self._cancel(user_id)
            await self.client.answer_callback_query(callback["id"])
            await self.sender.text(user_id, "История и поисковые фрагменты удалены из рабочей базы.", keyboard=mode_keyboard(state))
        elif action == "delete" and value == "no":
            if await self.store.accept_control(update_id):
                await self.client.answer_callback_query(callback["id"])
                await self.sender.text(user_id, "Удаление отменено.")
        else:
            await self.store.accept_control(update_id)
            await self.client.answer_callback_query(callback["id"], "Используйте /mode")

    def enqueue(self, job: TurnJob) -> None:
        if job.update_id in self.queued_ids:
            return
        self.queued_ids.add(job.update_id)
        queue = self.queues.setdefault(job.user_id, asyncio.Queue())
        queue.put_nowait(job)
        self.arrivals[job.update_id] = time.monotonic()
        if job.user_id not in self.workers or self.workers[job.user_id].done():
            self.workers[job.user_id] = asyncio.create_task(self._worker(job.user_id))

    async def _cancel(self, user_id: int) -> None:
        self.temporary.clear(user_id)
        for key in [key for key in self.web_requests if key[0] == user_id]:
            self.web_requests.pop(key, None)
        waiting = self.acquiring.get(user_id)
        active = self.active.get(user_id)
        if waiting:
            waiting.cancel()
        if active:
            active.cancel()
        # Drain synchronously before yielding, so the worker cannot extract
        # another private job while cancellation/SQL cleanup is in progress.
        cancelled_ids = []
        queue = self.queues.get(user_id)
        if queue:
            while not queue.empty():
                job = queue.get_nowait()
                self.arrivals.pop(job.update_id, None)
                self.queued_ids.discard(job.update_id)
                cancelled_ids.append(job.update_id)
                queue.task_done()
                del job
        worker = self.workers.get(user_id)
        for task in (waiting, active, worker):
            if task:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        for update_id in cancelled_ids:
            await self.store.finish_job(update_id, "cancelled")

    async def _worker(self, user_id: int) -> None:
        queue = self.queues[user_id]
        while not queue.empty() and not self.closed:
            job = await queue.get()
            acquired = False
            try:
                acquisition = asyncio.create_task(self.semaphore.acquire())
                self.acquiring[user_id] = acquisition
                if job.mode == Mode.CONFESSION:
                    remaining = self.settings.temporary_idle_seconds - (time.monotonic() - self.arrivals.get(job.update_id, time.monotonic()))
                    if job.channel == "web":
                        remaining = min(remaining, self.temporary.remaining(job.user_id, job.epoch))
                    if remaining <= 0:
                        acquisition.cancel()
                        with suppress(asyncio.CancelledError):
                            await acquisition
                        await self.store.finish_job(job.update_id, "expired")
                        continue
                    try:
                        await asyncio.wait_for(acquisition, timeout=remaining)
                    except TimeoutError:
                        await self.store.finish_job(job.update_id, "expired")
                        continue
                    except asyncio.CancelledError:
                        if self.closed:
                            raise
                        await self.store.finish_job(job.update_id, "cancelled")
                        continue
                else:
                    try:
                        await acquisition
                    except asyncio.CancelledError:
                        if self.closed:
                            raise
                        await self.store.finish_job(job.update_id, "cancelled")
                        continue
                acquired = True
                self.acquiring.pop(user_id, None)
                task = asyncio.create_task(self._process(job))
                self.active[user_id] = task
                with suppress(asyncio.CancelledError):
                    await task
            finally:
                self.acquiring.pop(user_id, None)
                if acquired:
                    self.semaphore.release()
                self.arrivals.pop(job.update_id, None)
                self.queued_ids.discard(job.update_id)
                self.active.pop(user_id, None)
                queue.task_done()
        # Do not retain completed per-user queues/tasks indefinitely.
        self.queues.pop(user_id, None)
        self.workers.pop(user_id, None)

    async def _typing(self, chat_id: int) -> None:
        while True:
            with suppress(TelegramError):
                await self.client.send_chat_action(chat_id)
            await asyncio.sleep(4)

    async def _process(self, job: TurnJob) -> None:
        if not await self.store.is_current(job):
            await self.store.finish_job(job.update_id, "cancelled")
            return
        typing = asyncio.create_task(self._typing(job.chat_id)) if job.channel == "telegram" else None
        try:
            if job.channel == "web":
                await self.store.finish_job(job.update_id, "running")
                record = self.web_requests.get((job.user_id, job.request_id))
                if record:
                    record["status"] = "running"
            history = None
            if job.mode == Mode.CONFESSION:
                if job.channel == "web":
                    history = self.temporary.peek(job.user_id, job.epoch)
                    if history is None:
                        await self.store.finish_job(job.update_id, "expired")
                        return
                    fresh = False
                else:
                    history, fresh = self.temporary.context(job.user_id, job.epoch)
                if fresh and job.channel == "telegram":
                    await self.sender.text(job.chat_id, TEMP_NOTICE)
            if job.channel == "web" and job.mode == Mode.CONFESSION:
                try:
                    reply = await asyncio.wait_for(self.turn.run(job, temporary_history=history), self.temporary.remaining(job.user_id, job.epoch))
                except TimeoutError:
                    self.temporary.clear(job.user_id)
                    self._prune_web_results()
                    await self.store.finish_job(job.update_id, "expired")
                    return
            else:
                reply = await self.turn.run(job, temporary_history=history)
            if not await self.store.is_current(job):
                await self.store.finish_job(job.update_id, "cancelled")
                return
            vector = None
            if job.mode != Mode.CONFESSION:
                vector = await self.turn.knowledge.embed(job.text)
            metadata = {"text": reply.body if reply.body is not None else reply.text,
                        "sources": [asdict(source) for source in reply.sources], "referral": reply.referral}
            if job.channel == "web" and job.mode == Mode.CONFESSION and self.temporary.peek(job.user_id, job.epoch) is None:
                await self.store.finish_job(job.update_id, "expired")
                return
            if not await self.store.complete(job, reply.text, vector, reply_metadata=metadata):
                return
            if job.mode == Mode.CONFESSION:
                self.temporary.append(job.user_id, job.epoch, job.text, reply.text,
                                      reply_metadata=metadata if job.channel == "web" else None)
            if job.channel == "web":
                # No await between the final epoch check and publication in RAM.
                if not await self.store.is_current(job):
                    return
                record = self.web_requests.get((job.user_id, job.request_id))
                if record:
                    record.update(status="done", reply=metadata)
                await self.store.finish_job(job.update_id, "done")
            else:
                await self._deliver(job, reply.text)
        except asyncio.CancelledError:
            if not self.closed:
                await self.store.finish_job(job.update_id, "cancelled")
            raise
        except TelegramError as exc:
            await self.store.finish_job(job.update_id, "blocked" if exc.status == 403 else "delivery_unknown")
            logger.warning("delivery_error status=%d", exc.status)
        except Exception as exc:
            # Never stringify an exception: provider bodies, SQL parameters and
            # validation errors can contain private text. Codes/classes only.
            logger.warning("turn_error kind=%s", type(exc).__name__)
            from pastoral_bot.turn import TurnError
            code = exc.code if isinstance(exc, TurnError) else "provider_unavailable"
            await self.store.finish_job(job.update_id, "error" if job.channel == "web" else "failed", error_code=code)
            record = self.web_requests.get((job.user_id, job.request_id))
            if record:
                record.update(status="error", error_code=code)
            if await self.store.is_current(job):
                text = exc.user_message if isinstance(exc, TurnError) else "Сейчас не удалось подготовить проверенный ответ. Попробуйте позже."
                if job.channel == "telegram":
                    with suppress(TelegramError):
                        await self.sender.text(job.chat_id, text)
        finally:
            if typing:
                typing.cancel()
                with suppress(asyncio.CancelledError):
                    await typing

    async def _deliver(self, job: TurnJob, text: str, start: int = 0) -> None:
        for index, part in enumerate(split_message(text)):
            if index < start:
                continue
            if not await self.store.is_current(job):
                await self.store.finish_job(job.update_id, "cancelled")
                return
            await self.sender.message(job.chat_id, part)
            await self.store.mark_delivered(job.update_id, index + 1)
        await self.store.finish_job(job.update_id, "sent")

    async def recover(self) -> None:
        for job, reply, delivered in await self.store.pending_deliveries():
            if await self.store.is_current(job):
                try:
                    await self._deliver(job, reply, delivered)
                except TelegramError as exc:
                    await self.store.finish_job(job.update_id, "blocked" if exc.status == 403 else "delivery_unknown")
        for job in await self.store.pending_jobs():
            self.enqueue(job)

    async def poll(self, *, recover: bool = True) -> None:
        if recover:
            await self.recover()
        while not self.closed:
            self.temporary.prune()
            self._prune_web_results()
            await self._prune_waiting()
            try:
                updates = await self.client.get_updates(await self.store.next_offset(), allowed_updates=["message", "callback_query"])
                for update in updates:
                    try:
                        await self.handle(update)
                    except TelegramError as exc:
                        logger.warning("telegram_control_error status=%d", exc.status)
            except TelegramError as exc:
                if exc.conflict:
                    raise RuntimeError("Another process is polling this bot token") from None
                logger.warning("poll_error status=%d", exc.status)
                await asyncio.sleep(max(1, float(exc.retry_after or 3)))

    async def _prune_waiting(self) -> None:
        now = time.monotonic()
        for queue in list(self.queues.values()):
            keep = []
            expired = []
            while not queue.empty():
                job = queue.get_nowait()
                queue.task_done()
                if job.mode == Mode.CONFESSION and (now - self.arrivals.get(job.update_id, now) >= self.settings.temporary_idle_seconds or (job.channel == "web" and self.temporary.peek(job.user_id, job.epoch) is None)):
                    self.arrivals.pop(job.update_id, None)
                    self.queued_ids.discard(job.update_id)
                    expired.append(job)
                else:
                    keep.append(job)
            for job in keep:
                queue.put_nowait(job)
            for job in expired:
                await self.store.finish_job(job.update_id, "expired")

    async def close(self) -> None:
        self.closed = True
        for task in list(self.active.values()) + list(self.workers.values()) + list(self.acquiring.values()):
            task.cancel()
        await asyncio.gather(*list(self.active.values()), *list(self.workers.values()), return_exceptions=True)
        for user_id in list(self.temporary.sessions):
            self.temporary.clear(user_id)
        self.queues.clear()
        self.arrivals.clear()
        self.queued_ids.clear()
        self.web_requests.clear()
        self.acquiring.clear()

    def _prune_web_results(self) -> None:
        for key, record in list(self.web_requests.items()):
            if self.temporary.peek(key[0], record["epoch"]) is None:
                self.web_requests.pop(key, None)

    @staticmethod
    def web_update_id(user_id: int, request_id: str) -> int:
        # Telegram update IDs are nonnegative. Include owner to prevent UUID
        # reuse by one user from colliding with another user's request.
        value = int.from_bytes(sha256(f"{user_id}:{request_id}".encode()).digest()[:8], "big") & ((1 << 63) - 1)
        return -(value or 1)

    async def web_state(self, user_id: int) -> dict:
        state = await self.store.get_user(user_id)
        self._prune_web_results()
        context = self.temporary.peek(user_id, state.epoch) if state.mode == Mode.CONFESSION else None
        if state.mode == Mode.CONFESSION:
            history = [{"id": item["id"], "role": item["role"],
                        "text": (item.get("reply_metadata") or {}).get("text", item["content"]),
                        "sources": (item.get("reply_metadata") or {}).get("sources", [])}
                       for item in (context or [])]
        else:
            history = await self.store.web_history(user_id, state.conversation_id)
        budget = getattr(self.turn, "budget", None)
        usage = await budget.usage(user_id) if budget else {"answered": 0, "pending": 0}
        return {"mode": state.mode.value, "epoch": state.epoch, "conversation_id": state.conversation_id,
                "history": history, "remaining_answers": max(0, self.settings.daily_answer_limit - usage["answered"] - usage["pending"]),
                "daily_answer_limit": self.settings.daily_answer_limit, "max_message_chars": self.settings.max_message_chars,
                "temporary": {"active": state.mode == Mode.CONFESSION and context is not None,
                              "expired": state.mode == Mode.CONFESSION and context is None,
                              "idle_seconds": self.settings.temporary_idle_seconds,
                              "max_seconds": self.settings.temporary_max_seconds,
                              "remaining_seconds": int(self.temporary.remaining(user_id, state.epoch)) if context is not None else 0}}

    async def web_control(self, user_id: int, expected_epoch: int, action: str, mode: Mode | None = None) -> dict | None:
        async with self.user_lock(user_id):
            state = await self.store.web_transition(user_id, expected_epoch, action, mode)
            if state is None:
                return None
            await self._cancel(user_id)
            if state.mode == Mode.CONFESSION and action in ("mode", "new"):
                self.temporary.start(user_id, state.epoch)
            return await self.web_state(user_id)

    async def web_submit(self, user_id: int, request_id: str, text: str, expected_epoch: int) -> tuple[dict, int]:
        async with self.user_lock(user_id):
            state = await self.store.get_user(user_id)
            if state.epoch != expected_epoch:
                return {"code": "stale_epoch"}, 409
            old = await self.web_result(user_id, request_id)
            if old["status"] != "expired":
                return old, 202
            update_id = self.web_update_id(user_id, request_id)
            if await self.store.receipt_status(update_id) is not None:
                return {"code": "request_expired"}, 409
            if state.mode == Mode.CONFESSION and self.temporary.peek(user_id, state.epoch) is None:
                return {"code": "temporary_expired"}, 409
            self._prune_web_results()
            if sum(q.qsize() for q in self.queues.values()) + len(self.workers) >= self.settings.max_pending_jobs or (self.queues.get(user_id) and self.queues[user_id].qsize() >= 8) or len(self.web_requests) >= self.settings.max_pending_jobs * 4:
                return {"code": "queue_full"}, 429
            job = await self.store.accept_message(update_id, user_id, user_id, text, channel="web", request_id=request_id, expected_epoch=expected_epoch)
            if job is None:
                return {"code": "stale_epoch"}, 409
            if state.mode == Mode.CONFESSION:
                self.temporary.touch(user_id, state.epoch)
                self.web_requests[user_id, request_id] = {"epoch": state.epoch, "status": "pending"}
            self.enqueue(job)
            return {"request_id": request_id, "status": "pending", "epoch": state.epoch}, 202

    async def web_result(self, user_id: int, request_id: str) -> dict:
        state = await self.store.get_user(user_id)
        self._prune_web_results()
        record = self.web_requests.get((user_id, request_id))
        if state.mode == Mode.CONFESSION:
            if record and record["epoch"] == state.epoch:
                return {"request_id": request_id, **record}
            return {"request_id": request_id, "status": "expired", "epoch": state.epoch}
        result = await self.store.web_result(user_id, request_id, state.epoch)
        return result or {"request_id": request_id, "status": "expired", "epoch": state.epoch}
