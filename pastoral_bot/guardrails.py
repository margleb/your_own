"""Deterministic service facts and role boundaries, never theological verdicts."""
from __future__ import annotations

import re

from .types import Mode, TurnReply


def _is_question(text: str) -> bool:
    return "?" in text or bool(re.search(r"\b(?:ли|как|где|нужно|надо|стоит|расскажи|объясни)\b", text))


def _privacy_request(text: str) -> bool:
    durable_request = bool(
        re.search(r"\b(?:сохран\w*|запи[сш]\w*)", text)
        and re.search(r"навсегда|постоянн\w*|журнал\w*|для отладк\w*", text)
    )
    conversation = bool(re.search(r"переписк|истор|бесед|сообщен|контекст|данн|подготовк|телеграм|telegram", text))
    no_storage_request = conversation and bool(re.search(r"\bне (?:сохраняй|записывай|храни)\b", text))
    regular_draft = re.search(r"\b(?:состав\w*|сформулир\w*|исправ\w*|напиш\w*|запиш\w*).{0,80}заметк", text)
    if regular_draft and not durable_request and not no_storage_request:
        return False
    storage_question = conversation and _is_question(text) and bool(
        re.search(r"хран|сохран|помн|аноним|конфиден|удал|перезапуск|записыва", text)
    )
    identity_question = _is_question(text) and "подготов" in text and bool(
        re.search(r"полн\w* им|домашн\w* адрес|мое имя|мой адрес", text)
    )
    forwarding_request = bool(re.search(r"\b(?:перешл\w*|отправ\w*)", text) and "священник" in text
                              and re.search(r"заметк|признан|переписк", text))
    return durable_request or storage_question or no_storage_request or identity_question or forwarding_request


def _privacy_reply(mode: Mode, settings) -> TurnReply:
    if mode == Mode.CONFESSION:
        current = "Сейчас выбран временный режим подготовки к исповеди: текст не записывается в базу, индекс или долговечную очередь. "
    else:
        current = (
            "Сейчас выбран обычный режим: сообщения сохраняются до /delete_history. "
            "Упоминание исповеди или просьба не сохранять не меняют режим. "
            "Перед дальнейшим личным рассказом выберите подготовку к исповеди через /mode. "
        )
    idle = settings.temporary_idle_seconds
    lifetime = settings.temporary_max_seconds
    idle_label = "30 минут" if idle == 1800 else f"{idle} секунд"
    lifetime_label = "2 часа" if lifetime == 7200 else f"{lifetime} секунд"
    return TurnReply(
        current +
        f"Временный контекст хранится только в оперативной памяти до {idle_label} бездействия, максимум {lifetime_label}, "
        "и исчезает после перезапуска; режим остаётся временным до вашего явного переключения. "
        "Содержание бесед не записывается в технические журналы ни в одном режиме. "
        "Я не обещаю вечного хранения и не пересылаю заметки священнику. Полное имя и домашний адрес для подготовки не нужны. "
        "Telegram сохраняет облачную переписку; /delete_history не удаляет сообщения из Telegram. "
        "Запросы модели идут через OpenRouter к провайдерам с политикой Zero Data Retention; это не обещание полной анонимности. "
        f"Резервные копии обычной истории зашифрованы и хранятся до {settings.backup_retention_days} дней; удаления повторяются при восстановлении. "
        "Вы можете сами отредактировать и сохранить свою заметку."
    )


def service_reply(text: str, mode: Mode, settings) -> TurnReply | None:
    """Answer only narrowly recognized service/role questions without a model."""
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    if _privacy_request(normalized):
        return _privacy_reply(mode, settings)
    if ("разрешительн" in normalized and "молитв" in normalized
            and re.search(r"\b(?:прочит\w*|прочт\w*|произнес\w*|соверш\w*)", normalized)):
        return TurnReply(
            "Я — ИИ-помощник, а не священник. Я не могу совершить таинство исповеди, "
            "произнести разрешительную молитву как совершение таинства или отпустить грехи. "
            "Для исповеди обратитесь к священнику. Здесь я могу помочь подготовить личную заметку.",
            referral="priest",
        )
    if "от имени бога" in normalized and re.search(r"\b(?:скажи|говор\w*|ответ\w*|ты)\b", normalized):
        return TurnReply(
            "Я — ИИ-помощник и не могу говорить от имени Бога или знать Его особое решение о вас. "
            "Могу помочь сформулировать вопрос для разговора со священником.", referral="priest",
        )
    if (mode == Mode.CONFESSION and "мысл" in normalized and "испов" in normalized
            and _is_question(normalized)
            and re.search(r"нуж|надо|долж|стоит|перечисл|каки|все|кажд|критери", normalized)):
        return TurnReply(
            "Я не могу определить, какие мысли вам следует исповедовать: это индивидуальный вопрос для священника. "
            "Нежелательная или навязчивая мысль сама по себе не означает намерения или виновности. "
            "Этому боту не нужны интимные подробности. Для подготовки вы можете сами выбрать, что включить в заметку, "
            "и свободно её исправить. Если сомневаетесь, обсудите вопрос со священником.", referral="priest",
        )
    return None
