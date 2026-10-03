"""Private Telegram dispatcher with per-user ordering and cancellation epochs."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress

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
        self.arrivals: dict[int, float] = {}
        self.closed = False

    async def handle(self, update: dict) -> None:
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
            await self.sender.text(user_id, INTRO, keyboard=mode_keyboard(state))
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
        queue = self.queues.setdefault(job.user_id, asyncio.Queue())
        queue.put_nowait(job)
        self.arrivals[job.update_id] = time.monotonic()
        if job.user_id not in self.workers or self.workers[job.user_id].done():
            self.workers[job.user_id] = asyncio.create_task(self._worker(job.user_id))

    async def _cancel(self, user_id: int) -> None:
        self.temporary.clear(user_id)
        active = self.active.get(user_id)
        if active:
            active.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await active
        queue = self.queues.get(user_id)
        if queue:
            while not queue.empty():
                job = queue.get_nowait()
                self.arrivals.pop(job.update_id, None)
                await self.store.finish_job(job.update_id, "cancelled")
                queue.task_done()

    async def _worker(self, user_id: int) -> None:
        queue = self.queues[user_id]
        while not queue.empty() and not self.closed:
            job = await queue.get()
            acquired = False
            try:
                if job.mode == Mode.CONFESSION:
                    remaining = self.settings.temporary_idle_seconds - (time.monotonic() - self.arrivals.get(job.update_id, time.monotonic()))
                    if remaining <= 0:
                        await self.store.finish_job(job.update_id, "expired")
                        continue
                    try:
                        await asyncio.wait_for(self.semaphore.acquire(), timeout=remaining)
                    except TimeoutError:
                        await self.store.finish_job(job.update_id, "expired")
                        continue
                else:
                    await self.semaphore.acquire()
                acquired = True
                task = asyncio.create_task(self._process(job))
                self.active[user_id] = task
                with suppress(asyncio.CancelledError):
                    await task
            finally:
                if acquired:
                    self.semaphore.release()
                self.arrivals.pop(job.update_id, None)
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
        typing = asyncio.create_task(self._typing(job.chat_id))
        try:
            history = None
            if job.mode == Mode.CONFESSION:
                history, fresh = self.temporary.context(job.user_id, job.epoch)
                if fresh:
                    await self.sender.text(job.chat_id, TEMP_NOTICE)
            reply = await self.turn.run(job, temporary_history=history)
            if not await self.store.is_current(job):
                await self.store.finish_job(job.update_id, "cancelled")
                return
            vector = None
            if job.mode != Mode.CONFESSION:
                vector = await self.turn.knowledge.embed(job.text)
            if not await self.store.complete(job, reply.text, vector):
                return
            if job.mode == Mode.CONFESSION:
                self.temporary.append(job.user_id, job.epoch, job.text, reply.text)
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
            await self.store.finish_job(job.update_id, "failed")
            if await self.store.is_current(job):
                from pastoral_bot.turn import TurnError
                text = exc.user_message if isinstance(exc, TurnError) else "Сейчас не удалось подготовить проверенный ответ. Попробуйте позже."
                with suppress(TelegramError):
                    await self.sender.text(job.chat_id, text)
        finally:
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

    async def poll(self) -> None:
        await self.recover()
        while not self.closed:
            self.temporary.prune()
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
                if job.mode == Mode.CONFESSION and now - self.arrivals.get(job.update_id, now) >= self.settings.temporary_idle_seconds:
                    self.arrivals.pop(job.update_id, None)
                    expired.append(job)
                else:
                    keep.append(job)
            for job in keep:
                queue.put_nowait(job)
            for job in expired:
                await self.store.finish_job(job.update_id, "expired")

    async def close(self) -> None:
        self.closed = True
        for task in list(self.active.values()) + list(self.workers.values()):
            task.cancel()
        await asyncio.gather(*list(self.active.values()), *list(self.workers.values()), return_exceptions=True)
        for user_id in list(self.temporary.sessions):
            self.temporary.clear(user_id)
        self.queues.clear()
        self.arrivals.clear()
