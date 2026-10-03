"""Local corpus tests use synthetic texts; no network/model/API calls."""
import copy
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from pastoral_bot.import_sources import (
    SourceImportError, _BOOKS, bundle_hash, load_manifest, parse_catechism_html,
    parse_document_html, parse_nt_fb2, validate_bundle,
)
from pastoral_bot.knowledge import Knowledge, KnowledgeUnavailable, SafeEmbedder, _bible_reference


class NoEmbedding:
    async def embed(self, value):
        return None


def synthetic_bundle(version="v1"):
    spec = load_manifest()["eucharist-2015"]
    return {
        "format": 1, "version": version,
        "documents": [{
            "source_key": spec["key"], "title": spec["title"], "edition": spec["edition"],
            "canonical_url": spec["canonical_url"],
            "passages": [{"locator": f"III, абзац {i}", "url": spec["canonical_url"], "text": f"Покаяние исповедь священник тестовый фрагмент {i}"} for i in range(1, 16)],
        }],
    }


@pytest_asyncio.fixture
async def knowledge():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    value = Knowledge(SimpleNamespace(engine=engine), embedder=NoEmbedding())
    await value.initialize()
    yield value
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_library_is_unavailable(knowledge):
    with pytest.raises(KnowledgeUnavailable, match="knowledge_empty"):
        await knowledge.search("исповедь")


@pytest.mark.asyncio
async def test_approved_import_search_and_idempotency(knowledge):
    bundle = synthetic_bundle()
    assert await knowledge.import_documents(bundle, approve_hash=bundle_hash(bundle)) == {"eucharist-2015": 15}
    sources = await knowledge.search("исповедь")
    assert len(sources) == 6
    assert all(source.source_id.startswith("eucharist-2015:v1:") for source in sources)
    assert sources[0].edition == load_manifest()["eucharist-2015"]["edition"]
    assert await knowledge.import_documents(bundle, approve_hash=bundle_hash(bundle)) == {"eucharist-2015": 0}
    assert await knowledge.search("совершенно неизвестное") == []


@pytest.mark.asyncio
async def test_new_version_deactivates_old_in_transaction(knowledge):
    first, second = synthetic_bundle(), synthetic_bundle("v2")
    await knowledge.import_documents(first, approve_hash=bundle_hash(first))
    await knowledge.import_documents(second, approve_hash=bundle_hash(second))
    assert await knowledge.approved_count() == 1
    assert all(source.source_id.startswith("eucharist-2015:v2:") for source in await knowledge.search("покаяние"))
    async with knowledge.engine.connect() as conn:
        assert len((await conn.execute(select(knowledge.documents))).all()) == 2


@pytest.mark.asyncio
async def test_wrong_approval_or_conflicting_version_never_changes_corpus(knowledge):
    bundle = synthetic_bundle()
    with pytest.raises(ValueError, match="approval_hash_mismatch"):
        await knowledge.import_documents(bundle, approve_hash="0" * 64)
    assert await knowledge.approved_count() == 0
    await knowledge.import_documents(bundle, approve_hash=bundle_hash(bundle))
    changed = copy.deepcopy(bundle)
    changed["documents"][0]["passages"][0]["text"] = "Изменено"
    with pytest.raises(ValueError, match="version_content_conflict"):
        await knowledge.import_documents(changed, approve_hash=bundle_hash(changed))
    assert await knowledge.approved_count() == 1


@pytest.mark.asyncio
async def test_unapproved_passages_never_retrieved(knowledge):
    async with knowledge.engine.begin() as conn:
        await conn.execute(insert(knowledge.documents).values(id="private", source_key="test", title="test", edition="test", canonical_url="https://example.org", version="v1", content_hash="0" * 64, approved=False))
        await conn.execute(insert(knowledge.passages).values(id="private:1", document_id="private", ordinal=1, locator="x", url="https://example.org", text="секретный чужой текст"))
    with pytest.raises(KnowledgeUnavailable):
        await knowledge.search("секретный")


@pytest.mark.asyncio
async def test_embedding_failures_do_not_log_private_text(knowledge, caplog):
    class Failing:
        async def embed(self, value):
            raise RuntimeError(value)
    knowledge.embedder = Failing()
    assert await knowledge.embed("НЕ_ЗАПИСЫВАТЬ_МОЁ_ПРИЗНАНИЕ") is None
    assert "НЕ_ЗАПИСЫВАТЬ" not in caplog.text


def test_bundle_rejects_incomplete_unknown_sources_urls_and_locators():
    validate_bundle(synthetic_bundle())
    for alter, code in [
        (lambda d: d.update(source_key="arbitrary-web-page"), "unknown_or_duplicate_source"),
        (lambda d: d.update(edition="неизвестная редакция"), "source_metadata_mismatch"),
        (lambda d: d["passages"].pop(), "source_content_incomplete"),
        (lambda d: d["passages"][0].update(url="https://evil.example"), "invalid_passage_url"),
        (lambda d: d["passages"][0].update(locator=d["passages"][1]["locator"]), "invalid_passage_locator"),
    ]:
        bundle = synthetic_bundle()
        alter(bundle["documents"][0])
        with pytest.raises(ValueError, match=code):
            validate_bundle(bundle)


def test_fb2_only_new_testament_precise_verse_addresses():
    def book(title):
        if title == "Послание Иуды":
            return f"<section><title><p>{title}</p></title><p><sup>1</sup> Тестовый стих.</p></section>"
        return f"<section><title><p>{title}</p></title><section><title><p>Глава 1</p></title><cite><p><sup><emphasis>1</emphasis></sup> Тестовый стих.</p></cite></section></section>"
    payload = ('<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body>' + book("Книга Бытие") + "".join(book(title) for title, _, _ in _BOOKS) + '</body><body name="notes"><section><p>Комментарий</p></section></body></FictionBook>').encode()
    passages = parse_nt_fb2(payload)
    assert len(passages) == 27
    assert passages[0] == {"locator": "Мф 1:1", "text": "Тестовый стих.", "url": "https://azbyka.ru/biblia/?Mt.1:1&r"}
    assert all("Комментарий" not in passage["text"] for passage in passages)
    with pytest.raises(SourceImportError, match="new_testament_books_missing"):
        parse_nt_fb2(b'<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><body/></FictionBook>')
    with pytest.raises(SourceImportError, match="unsafe_xml"):
        parse_nt_fb2(b'<!DOCTYPE x><FictionBook/>')


def test_catechism_imports_numbered_questions_not_navigation():
    payload = '<nav>Купить книгу</nav><div class="book"><h1>Введение</h1><a id="1_1"></a><h2>Понятия</h2><p class="h7">1. Тестовый вопрос?</p><p class="txt">Тестовый ответ.</p><p class="h7">2. Следующий вопрос?</p><p class="txt">Следующий ответ.</p></div>'.encode()
    url = load_manifest()["filaret-catechism-2013"]["fetch_url"] + "1"
    passages = parse_catechism_html(payload, url)
    assert [p["locator"] for p in passages] == ["Вопрос 1", "Вопрос 2"]
    assert passages[0]["url"] == url + "#1_1"
    assert "Купить" not in passages[0]["text"]
    assert "Тестовый ответ" in passages[0]["text"]


def test_document_parsers_require_known_content_and_keep_sections():
    payload = '<nav>Реклама</nav><main><div class="content"><p>III. Церковь и общество</p><p>III.1. Тестовое положение.</p><p>Продолжение.</p></div></main>'.encode()
    passages = parse_document_html(payload, parser="social_html", canonical_url="https://example.org")
    assert passages[1]["locator"] == "III.1, абзац 1"
    assert passages[2]["locator"] == "III.1, абзац 2"
    assert all("Реклама" not in passage["text"] for passage in passages)
    with pytest.raises(SourceImportError, match="document_content_missing"):
        parse_document_html(b"<html>Navigation only</html>", parser="eucharist_html", canonical_url="https://example.org")


def test_safe_embedder_has_no_legacy_import_or_error_logging(tmp_path, caplog):
    class FailingModel:
        def encode(self, values, **kwargs):
            raise RuntimeError(values)
    embedder = SafeEmbedder(tmp_path)
    embedder._model = FailingModel()
    assert embedder._encode_many(["ПРИВАТНОЕ_ПРИЗНАНИЕ"]) == [None]
    assert embedder.failure_code == "embedding_unavailable:RuntimeError"
    assert "ПРИВАТНОЕ_ПРИЗНАНИЕ" not in caplog.text


def test_explicit_bible_reference_normalizes_abbreviations():
    assert _bible_reference("Объясни Ин. 3:16") == "Ин 3:16"
    assert _bible_reference("1Пет. 2:3") == "1 Пет 2:3"
    assert _bible_reference("неизвестная книга 3:16") is None


@pytest.mark.asyncio
async def test_postgres_corpus_fts_vectors_in_disposable_database():
    if os.environ.get("PASTORAL_TEST_POSTGRES") != "1":
        pytest.skip("Set PASTORAL_TEST_POSTGRES=1 for isolated PostgreSQL integration")
    import asyncpg
    from sqlalchemy.engine import make_url
    from settings import settings

    class VectorEmbedding:
        async def embed(self, value):
            return [1.0] + [0.0] * 383

    url = make_url(settings.DATABASE_URL)
    name = "pastoral_test_" + uuid4().hex
    admin = await asyncpg.connect(user=url.username, password=url.password, host=url.host, port=url.port or 5432, database="postgres")
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(url.set(database=name).render_as_string(hide_password=False))
    corpus = Knowledge(engine, embedder=VectorEmbedding())
    try:
        await corpus.initialize()
        bundle = synthetic_bundle()
        await corpus.import_documents(bundle, approve_hash=bundle_hash(bundle))
        assert len(await corpus.search("исповедью священнику")) == 6
        # Semantic-only query exercises the native 384-dimensional vector bind.
        assert len(await corpus.search("размышление")) == 6
        # Empty query has no keywords; cosine still works.
        assert len(await corpus.search("")) == 6
    finally:
        await engine.dispose()
        # Generated random name only, never the configured/user database.
        await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()
