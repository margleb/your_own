from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from pastoral_bot.config import BotSettings
from pastoral_bot.llm import Completion, PastoralLLM
from pastoral_bot.turn import PastoralTurn, TurnError
from pastoral_bot.types import Mode, SourcePassage, TurnJob

SOURCE = SourcePassage("nt:john:3:16", "Евангелие от Иоанна", "Синодальный перевод", "3:16", "https://azbyka.ru/biblia/?Jn.3:16", "Ибо так возлюбил Бог мир...")


def response(text="Можно обсудить это со священником.", ids=None):
    return json.dumps({"text": text, "source_ids": ids if ids is not None else [SOURCE.source_id], "referral": "priest"}, ensure_ascii=False)


class Store:
    def __init__(self):
        self.current = True
        self.history_calls = []
        self.recall_calls = []

    async def is_current(self, job):
        return self.current

    async def history(self, user_id, conversation_id):
        self.history_calls.append((user_id, conversation_id))
        return [{"role": "user", "content": "Обычная сохранённая история"}, {"role": "assistant", "content": "Предыдущий ответ"}]

    async def recall(self, user_id, conversation_id, vector):
        self.recall_calls.append((user_id, conversation_id))
        return [{"role": "user", "content": "Личное прежнее воспоминание"}]


class Knowledge:
    def __init__(self):
        self.sources = [SOURCE]
        self.embed_calls = []

    async def search(self, text):
        return self.sources

    async def embed(self, text):
        self.embed_calls.append(text)
        return [0.5] * 384


class Budget:
    def __init__(self):
        self.reserved = []
        self.settled = []
        self.allowed = True

    async def reserve(self, user_id, amount):
        self.reserved.append((user_id, amount))
        return 1 if self.allowed else None

    async def settle(self, reservation, actual, *, answered):
        self.settled.append((reservation, actual, answered))


class FakeLLM(PastoralLLM):
    def __init__(self, settings):
        super().__init__(settings, client=object())
        self.outputs = [Completion(response(), Decimal("0.002"), "stop")]
        self.calls = []
        self.after_complete = None

    async def complete(self, messages):
        self.calls.append(messages)
        if self.after_complete:
            self.after_complete()
        result = self.outputs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def services():
    settings = BotSettings()
    store, knowledge, budget = Store(), Knowledge(), Budget()
    llm = FakeLLM(settings)
    return PastoralTurn(settings, store, knowledge, llm, budget), store, knowledge, llm, budget


def job(mode=Mode.TALK):
    return TurnJob(1, 123, 123, "conversation-a", 0, mode, "Меня тревожит мой поступок")


@pytest.mark.asyncio
async def test_normal_context_is_owned_and_server_renders_sources(services):
    turn, store, _, llm, budget = services
    result = await turn.run(job())
    assert store.history_calls == [(123, "conversation-a")]
    assert store.recall_calls == [(123, "conversation-a")]
    assert result.source_ids == [SOURCE.source_id]
    assert SOURCE.url in result.text
    assert SOURCE.edition in result.text
    assert llm.calls[0][-1]["content"] == job().text
    assert budget.settled == [(1, Decimal("0.002"), True)]


@pytest.mark.asyncio
async def test_temporary_mode_never_reads_ordinary_history_or_recall(services):
    turn, store, knowledge, llm, _ = services
    await turn.run(job(Mode.CONFESSION), [{"role": "user", "content": "Временная контрольная фраза"}])
    prompt = json.dumps(llm.calls[0], ensure_ascii=False)
    assert "Временная контрольная фраза" in prompt
    assert "Обычная сохранённая история" not in prompt
    assert "Личное прежнее воспоминание" not in prompt
    assert store.history_calls == store.recall_calls == knowledge.embed_calls == []


@pytest.mark.asyncio
async def test_invalid_source_gets_exactly_one_repair_and_one_quota_slot(services):
    turn, _, _, llm, budget = services
    llm.outputs = [Completion(response(ids=["made-up"]), Decimal("0.001"), "stop"), Completion(response(), Decimal("0.002"), "stop")]
    result = await turn.run(job(Mode.FAITH))
    assert result.source_ids == [SOURCE.source_id]
    assert len(llm.calls) == 2
    assert len(budget.reserved) == 1
    assert budget.settled == [(1, Decimal("0.003"), True)]
    assert "made-up" not in json.dumps(llm.calls[1], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "not json", response(ids=["unknown"]), response(text="Источник https://evil.example"),
    '{"text":"ответ","source_ids":[],"referral":"absolution"}',
])
async def test_invalid_reply_never_reaches_user_after_two_attempts(services, bad):
    turn, _, _, llm, budget = services
    llm.outputs = [Completion(bad, Decimal("0.001"), "stop")] * 2
    with pytest.raises(TurnError, match="invalid_reply"):
        await turn.run(job(Mode.FAITH))
    assert len(llm.calls) == 2
    assert budget.settled == [(1, Decimal("0.002"), False)]


@pytest.mark.asyncio
async def test_no_doctrinal_answer_without_vetted_sources(services):
    turn, _, knowledge, llm, budget = services
    knowledge.sources = []
    with pytest.raises(TurnError, match="no_sources"):
        await turn.run(job(Mode.FAITH))
    assert llm.calls == budget.reserved == []


@pytest.mark.asyncio
async def test_late_generation_after_mode_switch_cannot_be_answered(services):
    turn, store, _, llm, budget = services
    llm.after_complete = lambda: setattr(store, "current", False)
    with pytest.raises(TurnError, match="stale_turn"):
        await turn.run(job())
    assert budget.settled == [(1, Decimal("0.002"), False)]


@pytest.mark.asyncio
async def test_missing_cost_and_cancel_charge_full_reservation(services):
    turn, _, _, llm, budget = services
    llm.outputs = [Completion(response(), None, "stop")]
    await turn.run(job())
    assert budget.settled == [(1, None, True)]
    llm.outputs = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await turn.run(job())
    assert budget.settled[-1] == (1, None, False)


@pytest.mark.asyncio
async def test_budget_denial_does_not_call_provider(services):
    turn, _, _, llm, budget = services
    budget.allowed = False
    with pytest.raises(TurnError, match="limits_reached"):
        await turn.run(job())
    assert llm.calls == budget.settled == []


def test_content_is_hidden_from_contract_reprs():
    assert job().text not in repr(job())
    assert SOURCE.text not in repr(SOURCE)
    assert "private text" not in repr(Completion("private text"))


@pytest.mark.asyncio
async def test_real_store_corpus_budget_do_not_mix_users_or_persist_confession(tmp_path):
    from sqlalchemy import select

    from pastoral_bot.budget import Budget as RealBudget
    from pastoral_bot.import_sources import bundle_hash, load_manifest
    from pastoral_bot.knowledge import Knowledge as RealKnowledge
    from pastoral_bot.storage import Store as RealStore, metadata

    class LocalEmbedding:
        async def embed(self, text):
            return [1.0] + [0.0] * 383

    settings = BotSettings(state_dir=tmp_path)
    store = RealStore(f"sqlite+aiosqlite:///{tmp_path / 'isolated.db'}")
    try:
        await store.initialize()
        knowledge = RealKnowledge(store, settings, embedder=LocalEmbedding())
        await knowledge.initialize()
        spec = load_manifest()["eucharist-2015"]
        bundle = {"format": 1, "version": "integration-v1", "documents": [{
            "source_key": spec["key"], "title": spec["title"], "edition": spec["edition"],
            "canonical_url": spec["canonical_url"], "passages": [
                {"locator": f"III, абзац {i}", "url": spec["canonical_url"], "text": f"Исповедь покаяние священник проверяемый фрагмент {i}"}
                for i in range(1, 16)
            ],
        }]}
        await knowledge.import_documents(bundle, approve_hash=bundle_hash(bundle))
        source_id = (await knowledge.search("исповедь"))[0].source_id
        llm = FakeLLM(settings)
        llm.outputs = [Completion(response(ids=[source_id]), Decimal("0.001"), "stop")] * 4
        turn = PastoralTurn(settings, store, knowledge, llm, RealBudget(store, settings))
        alice = await store.accept_message(1, 101, 101, "АЛИСА_ЛИЧНАЯ_ИСТОРИЯ исповедь")
        bob = await store.accept_message(2, 202, 202, "БОБ_ЛИЧНАЯ_ИСТОРИЯ исповедь")
        for accepted in (alice, bob):
            reply = await turn.run(accepted)
            assert await store.complete(accepted, reply.text, [1.0] + [0.0] * 383)
        await store.new_conversation(101)
        again = await store.accept_message(3, 101, 101, "Как подготовиться к исповеди?")
        await turn.run(again)
        prompt = json.dumps(llm.calls[-1], ensure_ascii=False)
        assert "АЛИСА_ЛИЧНАЯ_ИСТОРИЯ" in prompt
        assert "БОБ_ЛИЧНАЯ_ИСТОРИЯ" not in prompt
        await store.set_mode(101, Mode.CONFESSION)
        secret = "ВРЕМЕННАЯ_УНИКАЛЬНАЯ_ФРАЗА исповедь"
        temporary = await store.accept_message(4, 101, 101, secret)
        reply = await turn.run(temporary)
        assert await store.complete(temporary, reply.text)
        prompt = json.dumps(llm.calls[-1], ensure_ascii=False)
        assert "АЛИСА_ЛИЧНАЯ_ИСТОРИЯ" not in prompt
        assert "БОБ_ЛИЧНАЯ_ИСТОРИЯ" not in prompt
        async with store.engine.connect() as conn:
            for table in metadata.sorted_tables:
                assert secret not in repr((await conn.execute(select(table))).all()), table.name
    finally:
        await store.close()
