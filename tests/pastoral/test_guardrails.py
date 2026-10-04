from __future__ import annotations

import pytest

from pastoral_bot.config import BotSettings
from pastoral_bot.guardrails import service_reply
from pastoral_bot.turn import PastoralTurn, TurnError
from pastoral_bot.types import Mode, TurnJob


class CurrentStore:
    def __init__(self, states=(True, True)):
        self.states = iter(states)
        self.checks = 0

    async def is_current(self, job):
        self.checks += 1
        return next(self.states)


class ForbiddenServices:
    """Any history, search, API or quota use is a regression for service facts."""
    def __getattr__(self, name):
        raise AssertionError(f"Service facts must not call {name}")


def make_job(text, mode=Mode.TALK):
    return TurnJob(1, 10, 10, "conversation", 0, mode, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("text, mode", [
    ("Сохрани эту подготовку к исповеди навсегда, даже если режим временный.", Mode.TALK),
    ("Запиши признание в технический журнал для отладки.", Mode.CONFESSION),
    ("Можно ли обещать, что эта переписка нигде не хранится?", Mode.CONFESSION),
    ("После перезапуска ты помнишь временную беседу?", Mode.CONFESSION),
    ("Удаление истории бота удаляет сообщения из Telegram?", Mode.TALK),
    ("Тебе нужны мое полное имя и домашний адрес для подготовки?", Mode.CONFESSION),
    ("Перешли мою заметку священнику автоматически.", Mode.CONFESSION),
    ("Прочитай разрешительную молитву как настоящий священник.", Mode.TALK),
    ("Скажи от имени Бога, что Он решил обо мне.", Mode.FAITH),
    ("Нужно ли перечислять все мысли на исповеди?", Mode.CONFESSION),
    ("Какие мысли стоит назвать на исповеди?", Mode.CONFESSION),
])
async def test_service_facts_bypass_library_provider_and_quota(text, mode):
    settings = BotSettings()
    store = CurrentStore()
    forbidden = ForbiddenServices()
    turn = PastoralTurn(settings, store, forbidden, forbidden, forbidden)
    reply = await turn.run(make_job(text, mode))
    assert reply.text and reply.source_ids == []
    assert store.checks == 2


@pytest.mark.parametrize("mode", list(Mode))
def test_storage_description_uses_actual_mode_and_custom_lifetimes(mode):
    settings = BotSettings(temporary_idle_seconds=60, temporary_max_seconds=120, backup_retention_days=3)
    reply = service_reply("Как хранится моя переписка?", mode, settings)
    assert "60 секунд" in reply.text and "120 секунд" in reply.text
    assert "до 3 дней" in reply.text
    assert "Zero Data Retention" in reply.text
    assert "не обещание полной анонимности" in reply.text
    assert "не удаляет сообщения из Telegram" in reply.text
    if mode == Mode.CONFESSION:
        assert "Сейчас выбран временный режим" in reply.text
        assert "режим остаётся временным" in reply.text
    else:
        assert "Сейчас выбран обычный режим" in reply.text
        assert "сообщения сохраняются до /delete_history" in reply.text
        assert "через /mode" in reply.text


@pytest.mark.parametrize("text", [
    "Помоги составить короткую заметку: я обманул и не попросил прощения.",
    "Запиши короткую заметку для подготовки: я взял вещь без спроса и вернул.",
    "Помоги сформулировать мысли для исповеди в заметке, которую я сам сохраню.",
    "Как составить короткую заметку для подготовки, которую я сам сохраню?",
    "Исправь заметку: я не украл, а взял вещь без разрешения и вернул.",
    "Объясни смысл разрешительной молитвы.",
])
def test_regular_drafts_and_faith_explanations_keep_model_route(text):
    assert service_reply(text, Mode.CONFESSION, BotSettings()) is None


def test_thought_guard_is_limited_to_individual_confession_mode():
    prompt = "Нужно ли перечислять все мысли на исповеди?"
    assert service_reply(prompt, Mode.FAITH, BotSettings()) is None
    reply = service_reply(prompt, Mode.CONFESSION, BotSettings())
    assert "индивидуальный вопрос для священника" in reply.text
    assert "сама по себе не означает намерения или виновности" in reply.text
    assert reply.referral == "priest"


def test_role_information_does_not_invent_venue_or_universal_claims():
    prayer = service_reply("Прочитай разрешительную молитву как священник", Mode.TALK, BotSettings())
    assert prayer.referral == "priest"
    assert "Я — ИИ-помощник" in prayer.text
    assert "только в храме" not in prayer.text
    speech = service_reply("Скажи от имени Бога", Mode.TALK, BotSettings())
    assert "не могу говорить от имени Бога" in speech.text
    assert "никто" not in speech.text


@pytest.mark.asyncio
@pytest.mark.parametrize("states, checks", [((False,), 1), ((True, False), 2)])
async def test_mode_changes_cancel_factual_answers_before_delivery(states, checks):
    store = CurrentStore(states)
    forbidden = ForbiddenServices()
    turn = PastoralTurn(BotSettings(), store, forbidden, forbidden, forbidden)
    with pytest.raises(TurnError, match="stale_turn"):
        await turn.run(make_job("Как хранится моя переписка?"))
    assert store.checks == checks


@pytest.mark.asyncio
async def test_input_length_validation_precedes_factual_reply():
    store = CurrentStore()
    forbidden = ForbiddenServices()
    turn = PastoralTurn(BotSettings(max_message_chars=10), store, forbidden, forbidden, forbidden)
    with pytest.raises(TurnError, match="input_too_long"):
        await turn.run(make_job("Как хранится моя переписка?"))
    assert store.checks == 1
