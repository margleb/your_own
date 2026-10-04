"""Preparation retrieval keeps the approved requirements and their exceptions."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from pastoral_bot.knowledge import Knowledge

LOCATORS = ("II, абзац 1", "II, абзац 2", "II, абзац 5", "II, абзац 11", "III, абзац 1", "III, абзац 2")
TEXTS = (
    "Требования подготовки применяются духовником с учетом состояния человека.",
    "Если нет духовника, обращайтесь к священникам храма, где желаете причаститься.",
    "Пост предваряет причащение; при заболеваниях может быть облегчен или отменен.",
    "Молитвенная подготовка включает Последование; правило может быть заменено по благословению.",
    "Готовящийся исповедует грехи священнику в Таинстве Покаяния.",
    "Духовник может благословить несколько причащений без исповеди перед каждым.",
)


class NoEmbedding:
    async def embed(self, value):
        return None


@pytest_asyncio.fixture
async def corpus():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    knowledge = Knowledge(SimpleNamespace(engine=engine), embedder=NoEmbedding())
    await knowledge.initialize()
    async with engine.begin() as conn:
        for edition, approved in [("reviewed-new-edition", True), ("retired-edition", False)]:
            await conn.execute(insert(knowledge.documents).values(
                id=edition, source_key="eucharist-2015", title="Об участии верных в Евхаристии",
                edition=edition, canonical_url="https://patriarchia.ru/document/96198",
                version=edition, content_hash="0" * 64, approved=approved,
            ))
            await conn.execute(insert(knowledge.passages), [dict(
                id=f"{edition}:{number}", document_id=edition, ordinal=number,
                locator=locator, text=f"{edition} {content}", url="https://patriarchia.ru/document/96198",
            ) for number, (locator, content) in enumerate(zip(LOCATORS, TEXTS), 1)])
            await conn.execute(insert(knowledge.passages).values(
                id=f"{edition}:99", document_id=edition, ordinal=99, locator="I, абзац 6",
                text="История подготовки к причастию в Российской империи.", url="https://patriarchia.ru/document/96198",
            ))
        await conn.execute(insert(knowledge.documents).values(
            id="nt", source_key="new-testament-synodal", title="Новый Завет", edition="Синодальный",
            canonical_url="https://azbyka.ru/biblia/", version="nt", content_hash="0" * 64, approved=True,
        ))
        await conn.execute(insert(knowledge.passages).values(
            id="nt:16", document_id="nt", ordinal=1, locator="Ин 3:16",
            text="Ибо так возлюбил Бог мир.", url="https://azbyka.ru/biblia/?Jn.3:16&r",
        ))
    yield knowledge
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["Как подготовиться к причастию?", "Подготовка к Евхаристии", "Какова подготовка к причащению?"])
async def test_preparation_keeps_approved_requirements_priest_and_exceptions(corpus, question):
    sources = await corpus.search(question)
    assert tuple(p.locator for p in sources) == LOCATORS
    assert [p.source_id for p in sources] == [f"reviewed-new-edition:{n}" for n in range(1, 7)]
    assert all(p.edition == "reviewed-new-edition" for p in sources)
    assert all(p.text.endswith(text) for p, text in zip(sources, TEXTS))
    assert all("Российской империи" not in p.text for p in sources)
    assert "заболеваниях" in sources[2].text
    assert "без исповеди" in sources[-1].text


@pytest.mark.asyncio
async def test_explicit_scripture_reference_wins_over_preparation_topic(corpus):
    sources = await corpus.search("Объясни Ин 3:16 при подготовке к причастию")
    assert sources[0].source_id == "nt:16"
    assert sources[0].locator == "Ин 3:16"


@pytest.mark.asyncio
async def test_missing_approved_requirement_is_not_filled_from_retired_edition(corpus):
    async with corpus.engine.begin() as conn:
        await conn.execute(corpus.passages.delete().where(corpus.passages.c.id == "reviewed-new-edition:3"))
    sources = await corpus.search("Как подготовиться к причастию?")
    assert all(p.edition != "retired-edition" for p in sources)
    assert all(p.locator != "II, абзац 5" for p in sources)


@pytest.mark.asyncio
async def test_unrelated_preparation_question_keeps_general_retrieval(corpus):
    sources = await corpus.search("Как подготовиться к собеседованию?")
    assert tuple(p.locator for p in sources) != LOCATORS
