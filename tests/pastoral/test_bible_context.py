"""Retrieval keeps complete scripture evidence and its approved edition."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from pastoral_bot.knowledge import Knowledge


class NoEmbedding:
    async def embed(self, value):
        return None


@pytest_asyncio.fixture
async def corpus():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    knowledge = Knowledge(SimpleNamespace(engine=engine), embedder=NoEmbedding())
    await knowledge.initialize()
    async with engine.begin() as conn:
        for edition, approved in [("approved", True), ("old", False)]:
            await conn.execute(insert(knowledge.documents).values(
                id=edition, source_key="new-testament-synodal", title="Новый Завет", edition=edition,
                canonical_url="https://azbyka.ru/biblia/", version=edition, content_hash="0" * 64, approved=approved,
            ))
            await conn.execute(insert(knowledge.passages), [dict(
                id=f"{edition}:{verse}", document_id=edition, ordinal=verse, locator=f"Лк 15:{verse}",
                text=f"{edition} Текст стиха {verse}.", url=f"https://azbyka.ru/biblia/?Lk.15:{verse}&r",
            ) for verse in range(11, 33)])
    yield knowledge
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Объясни притчу о блудном сыне", "Прочитай Лк. 15:11–32"])
async def test_story_and_range_include_every_verse_only_from_approved_edition(corpus, question):
    passages = await corpus.search(question)
    assert len(passages) == 1
    passage = passages[0]
    assert passage.locator == "Лк 15:11–32"
    assert passage.edition == "approved"
    assert passage.url == "https://azbyka.ru/biblia/?Lk.15:11-32&r"
    assert len(passage.text.splitlines()) == 22
    assert "old" not in passage.text
    assert passage.text.endswith("approved Текст стиха 32.")


@pytest.mark.asyncio
async def test_explicit_single_verse_is_not_overridden_by_named_story(corpus):
    passages = await corpus.search("В притче о блудном сыне объясни Лк 15:20")
    assert passages[0].locator == "Лк 15:20"
    assert passages[0].source_id == "approved:20"


@pytest.mark.asyncio
async def test_incomplete_range_is_not_presented_as_complete(corpus):
    async with corpus.engine.begin() as conn:
        await conn.execute(corpus.passages.delete().where(corpus.passages.c.id == "approved:20"))
    passages = await corpus.search("Объясни притчу о блудном сыне")
    assert all("range:" not in p.source_id for p in passages)
