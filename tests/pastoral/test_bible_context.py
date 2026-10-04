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


@pytest.mark.asyncio
async def test_do_not_judge_uses_its_complete_approved_context(corpus):
    async with corpus.engine.begin() as conn:
        for edition in ("approved", "old"):
            await conn.execute(insert(corpus.passages), [dict(
                id=f"{edition}:mt7:{verse}", document_id=edition, ordinal=verse,
                locator=f"Мф 7:{verse}", text=f"{edition} Не судите, контекст стиха {verse}.",
                url=f"https://azbyka.ru/biblia/?Mt.7:{verse}&r",
            ) for verse in range(1, 6)])
    sources = await corpus.search("Что означает заповедь «не судите»?")
    assert len(sources) == 1
    assert sources[0].locator == "Мф 7:1–5"
    assert sources[0].url == "https://azbyka.ru/biblia/?Mt.7:1-5&r"
    assert len(sources[0].text.splitlines()) == 5
    assert "old" not in sources[0].text
    explicit = await corpus.search("Объясни «не судите» в Мф 7:3")
    assert explicit[0].source_id == "approved:mt7:3"


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Нужно ли прощать человека, который не просит прощения?", "Почему нужно прощать людям?"])
async def test_forgiving_people_returns_complete_prayer_context_not_isolated_sadness_verse(corpus, question):
    async with corpus.engine.begin() as conn:
        for edition in ("approved", "old"):
            await conn.execute(insert(corpus.passages), [dict(
                id=f"{edition}:mt6:{verse}", document_id=edition, ordinal=100 + verse,
                locator=f"Мф 6:{verse}", text=f"{edition} Прощение и молитва, стих {verse}.",
                url=f"https://azbyka.ru/biblia/?Mt.6:{verse}&r",
            ) for verse in range(12, 16)])
        await conn.execute(insert(corpus.passages).values(
            id="approved:cor2:7", document_id="approved", ordinal=200,
            locator="2 Кор 2:7", text="Простить человека, дабы он не был поглощен чрезмерною печалью.",
            url="https://azbyka.ru/biblia/?2Cor.2:7&r",
        ))
    sources = await corpus.search(question)
    assert len(sources) == 1
    assert sources[0].locator == "Мф 6:12–15"
    assert sources[0].url == "https://azbyka.ru/biblia/?Mt.6:12-15&r"
    assert len(sources[0].text.splitlines()) == 4
    assert "old" not in sources[0].text
    assert "печаль" not in sources[0].text
    explicit = await corpus.search("Нужно ли прощать человека? Объясни 2 Кор 2:7")
    assert explicit[0].source_id == "approved:cor2:7"
