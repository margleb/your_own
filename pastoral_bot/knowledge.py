"""Approved, versioned corpus; no personal database, settings or dialogue logs."""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import threading
from pathlib import Path

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint, cast, func, insert, literal, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from .types import SourcePassage


class KnowledgeUnavailable(RuntimeError):
    def __init__(self, code: str = "knowledge_unavailable"):
        super().__init__(code)


class SafeEmbedder:
    """The existing 384-dimensional model with isolated cache and safe failures."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._model = None
        self._failed = False
        self.failure_code: str | None = None
        self._lock = threading.Lock()

    def _encode_many(self, values: list[str]) -> list[list[float] | None]:
        with self._lock:
            if self._failed:
                return [None] * len(values)
            try:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(
                        "paraphrase-multilingual-MiniLM-L12-v2",
                        cache_folder=str(self.cache_dir),
                        device="cpu",
                    )
                vectors = self._model.encode(values, batch_size=32, show_progress_bar=False).tolist()
                self.failure_code = None
                return [vector if len(vector) == 384 and all(math.isfinite(x) for x in vector) else None for vector in vectors]
            except Exception as error:
                # Neither exception repr nor the input is ever logged.
                self._failed = self._model is None
                self.failure_code = "embedding_unavailable:" + type(error).__name__
                return [None] * len(values)

    async def embed(self, value: str) -> list[float] | None:
        return (await self.embed_many([value]))[0]

    async def embed_many(self, values: list[str]) -> list[list[float] | None]:
        return await asyncio.to_thread(self._encode_many, values)


def _tables():
    # Imported lazily: the storage module does not create a global engine.
    from .storage import vector_type

    metadata = MetaData()
    documents = Table(
        "pastoral_documents", metadata,
        Column("id", String(180), primary_key=True),
        Column("source_key", String(100), nullable=False),
        Column("title", Text, nullable=False),
        Column("edition", Text, nullable=False),
        Column("canonical_url", Text, nullable=False),
        Column("version", String(80), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("approved", Boolean, nullable=False, default=False),
        UniqueConstraint("source_key", "version"),
    )
    passages = Table(
        "pastoral_passages", metadata,
        Column("id", String(240), primary_key=True),
        Column("document_id", String(180), ForeignKey("pastoral_documents.id", ondelete="CASCADE"), nullable=False, index=True),
        Column("ordinal", Integer, nullable=False),
        Column("locator", Text, nullable=False),
        Column("url", Text, nullable=False),
        Column("text", Text, nullable=False),
        Column("embedding", vector_type(), nullable=True),
        UniqueConstraint("document_id", "ordinal"),
    )
    return metadata, documents, passages


class Knowledge:
    def __init__(self, store, settings=None, *, embedder=None, limit: int = 6):
        self.engine = store if isinstance(store, AsyncEngine) else store.engine
        self.settings = settings
        cache_dir = Path(settings.state_dir) / "embedding-cache" if settings else Path("/var/lib/pastoral-bot/embedding-cache")
        self.embedder = embedder if embedder is not None else SafeEmbedder(cache_dir)
        self.limit = limit
        self.metadata, self.documents, self.passages = _tables()

    async def initialize(self) -> None:
        async with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(self.metadata.create_all)
            if self.engine.dialect.name == "postgresql":
                await conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_pastoral_passages_russian "
                    "ON pastoral_passages USING gin (to_tsvector('russian', text))"
                ))

    async def embed(self, value: str) -> list[float] | None:
        try:
            call = self.embedder.embed(value) if hasattr(self.embedder, "embed") else self.embedder(value)
            vector = await call if hasattr(call, "__await__") else call
            if vector is None or len(vector) != 384 or not all(math.isfinite(x) for x in vector):
                return None
            return vector
        except Exception:
            return None

    async def approved_count(self) -> int:
        async with self.engine.connect() as conn:
            count = await conn.scalar(select(func.count()).select_from(self.documents).where(self.documents.c.approved.is_(True)))
            return int(count or 0)

    async def search(self, value: str) -> list[SourcePassage]:
        """Combine Russian FTS and cosine candidates from approved versions only."""
        try:
            if not await self.approved_count():
                raise KnowledgeUnavailable("knowledge_empty")
            vector = await self.embed(value)
            async with self.engine.connect() as conn:
                bible_range = _bible_range(value)
                if bible_range:
                    book, chapter, start, end = bible_range
                    locators = [f"{book} {chapter}:{verse}" for verse in range(start, end + 1)]
                    range_rows = (await conn.execute(self._base_query().where(
                        self.documents.c.source_key == "new-testament-synodal",
                        self.passages.c.locator.in_(locators),
                    ))).mappings().all()
                    # A complete passage is more useful than scattered verses
                    # for a named parable or an explicitly requested range.
                    by_locator = {row["locator"]: row for row in range_rows}
                    if all(locator in by_locator for locator in locators):
                        ordered = [by_locator[locator] for locator in locators]
                        first = ordered[0]
                        url = re.sub(r":\d+(?=&|$)", f":{start}-{end}", first["url"])
                        return [SourcePassage(
                            first["id"] + f":range:{start}-{end}", first["title"], first["edition"],
                            f"{book} {chapter}:{start}\u2013{end}", url,
                            "\n".join(f"{row['locator']} {row['text']}" for row in ordered),
                        )]
                document_topic = _document_topic(value)
                if document_topic:
                    source_key, locators = document_topic
                    topic_rows = (await conn.execute(self._base_query().where(
                        self.documents.c.source_key == source_key,
                        self.passages.c.locator.in_(locators),
                    ))).mappings().all()
                    by_locator = {row["locator"]: row for row in topic_rows}
                    # These approved paragraphs together retain requirements,
                    # pastoral adaptation and exceptions. Do not substitute
                    # older editions or unrelated semantic matches for a gap.
                    if all(locator in by_locator for locator in locators):
                        return [SourcePassage(
                            row["id"], row["title"], row["edition"], row["locator"], row["url"], row["text"],
                        ) for row in (by_locator[locator] for locator in locators[:min(6, self.limit)])]
                reference = _bible_reference(value)
                exact = []
                if reference:
                    exact = (await conn.execute(self._base_query().where(
                        self.documents.c.source_key == "new-testament-synodal",
                        self.passages.c.locator == reference,
                    ))).mappings().all()
                if self.engine.dialect.name == "postgresql":
                    rows = await self._postgres_candidates(conn, value, vector)
                else:
                    # Deterministic SQLite fallback is only for isolated unit tests.
                    rows = (await conn.execute(self._base_query())).mappings().all()
                    query_tokens = _tokens(value)
                    ranked = []
                    for row in rows:
                        overlap = len(query_tokens & _tokens(row["text"])) / max(1, len(query_tokens))
                        semantic = _cosine(vector, row["embedding"]) if vector is not None and row["embedding"] is not None else 0.0
                        if overlap or semantic >= 0.35:
                            ranked.append((overlap + semantic, row))
                    rows = [r for _, r in sorted(ranked, key=lambda pair: (-pair[0], pair[1]["id"]))[:self.limit]]
            exact_ids = {row["id"] for row in exact}
            rows = (list(exact) + [row for row in rows if row["id"] not in exact_ids])[:self.limit]
            return [SourcePassage(row["id"], row["title"], row["edition"], row["locator"], row["url"], row["text"]) for row in rows]
        except KnowledgeUnavailable:
            raise
        except Exception:
            raise KnowledgeUnavailable() from None

    def _base_query(self):
        p, d = self.passages.c, self.documents.c
        return select(p.id, p.locator, p.url, p.text, p.embedding, d.title, d.edition).select_from(
            self.passages.join(self.documents, p.document_id == d.id)
        ).where(d.approved.is_(True))

    async def _postgres_candidates(self, conn, value: str, vector):
        p = self.passages.c
        tokens = _tokens(value)
        candidates = {}
        if tokens:
            query = func.websearch_to_tsquery("russian", " OR ".join(sorted(tokens)))
            document = func.to_tsvector("russian", p.text)
            score = func.ts_rank_cd(document, query)
            result = await conn.execute(self._base_query().add_columns(score.label("score")).where(
                document.op("@@")(query)
            ).order_by(score.desc(), p.id).limit(self.limit * 2))
            for rank, row in enumerate(result.mappings()):
                candidates[row["id"]] = [1 / (40 + rank), row]
        if vector is not None:
            from .storage import Vector384
            operand = cast(literal(vector, type_=Vector384()), Vector384())
            distance = p.embedding.op("<=>", return_type=Float)(operand)
            result = await conn.execute(self._base_query().add_columns(distance.label("distance")).where(
                p.embedding.is_not(None), distance <= 0.65
            ).order_by(distance, p.id).limit(self.limit * 2))
            for rank, row in enumerate(result.mappings()):
                weight = 1 / (40 + rank)
                if row["id"] in candidates:
                    candidates[row["id"]][0] += weight
                else:
                    candidates[row["id"]] = [weight, row]
        return [r for _, r in sorted(candidates.values(), key=lambda pair: (-pair[0], pair[1]["id"]))[:self.limit]]

    async def import_documents(self, bundle: dict, *, approve_hash: str) -> dict[str, int]:
        """One transaction; reviewed hash binds approval to exact text/locators."""
        from .import_sources import bundle_hash, validate_bundle

        validate_bundle(bundle)
        if not approve_hash or approve_hash != bundle_hash(bundle):
            raise ValueError("approval_hash_mismatch")
        results = {}
        # Build embeddings before opening the write transaction. Public texts only.
        prepared = []
        async with self.engine.connect() as conn:
            existing_ids = set((await conn.execute(select(self.documents.c.id))).scalars())
        for document in bundle["documents"]:
            passages = document["passages"]
            embeddings = [None] * len(passages)
            if f"{document['source_key']}:{bundle['version']}" not in existing_ids:
                # Reuse vectors only for identical public source text. Locator
                # corrections do not require encoding the entire corpus again.
                async with self.engine.connect() as conn:
                    previous = (await conn.execute(select(self.passages.c.text, self.passages.c.embedding).select_from(
                        self.passages.join(self.documents)
                    ).where(self.documents.c.source_key == document["source_key"], self.passages.c.embedding.is_not(None)))).all()
                cached = {value: vector for value, vector in previous}
                embeddings = [cached.get(p["text"]) for p in passages]
                missing = [number for number, vector in enumerate(embeddings) if vector is None]
                for start in range(0, len(missing), 64):
                    indices = missing[start:start + 64]
                    batch = [passages[number]["text"] for number in indices]
                    if hasattr(self.embedder, "embed_many"):
                        vectors = await self.embedder.embed_many(batch)
                    else:
                        vectors = [await self.embed(value) for value in batch]
                    if len(vectors) != len(batch):
                        raise ValueError("invalid_embedding_batch")
                    for number, vector in zip(indices, vectors):
                        embeddings[number] = vector
            prepared.append((document, embeddings))
        async with self.engine.begin() as conn:
            for document, embeddings in prepared:
                key, version = document["source_key"], bundle["version"]
                doc_id = f"{key}:{version}"
                digest = hashlib.sha256("\n".join(p["locator"] + "\0" + p["text"] + "\0" + p["url"] for p in document["passages"]).encode()).hexdigest()
                existing = (await conn.execute(select(self.documents).where(self.documents.c.id == doc_id))).mappings().first()
                if existing:
                    if existing["content_hash"] != digest:
                        raise ValueError("version_content_conflict")
                    results[key] = 0
                    continue
                await conn.execute(update(self.documents).where(self.documents.c.source_key == key).values(approved=False))
                await conn.execute(insert(self.documents).values(
                    id=doc_id, source_key=key, title=document["title"], edition=document["edition"],
                    canonical_url=document["canonical_url"], version=version, content_hash=digest, approved=True,
                ))
                rows = [dict(
                    id=f"{doc_id}:{number}", document_id=doc_id, ordinal=number, locator=p["locator"],
                    url=p["url"], text=p["text"], embedding=embeddings[number - 1],
                ) for number, p in enumerate(document["passages"], 1)]
                # Bound executemany packet size for the full New Testament.
                for start in range(0, len(rows), 250):
                    await conn.execute(insert(self.passages), rows[start:start + 250])
                results[key] = len(rows)
        return results


_STOP_WORDS = frozenset("как что это мне меня мой моя мои мы вы они его ее если или для чтобы о об и а но на в во с со к у из за по от не ли бы быть есть скажи пожалуйста почему можно нужно такой так".split())


def _tokens(value: str) -> set[str]:
    return {word for word in re.findall(r"[а-яёa-z0-9]+", value.lower()) if len(word) > 2 and word not in _STOP_WORDS}


def _bible_reference(value: str) -> str | None:
    match = re.search(r"\b((?:[123]\s*)?(?:Мф|Мк|Лк|Ин|Деян|Иак|Пет|Иуд|Рим|Кор|Гал|Еф|Флп|Кол|Фес|Тим|Тит|Флм|Евр|Откр))\.?\s*(\d+)\s*:\s*(\d+)", value, re.IGNORECASE)
    if not match:
        return None
    short = re.sub(r"^([123])\s*", r"\1 ", match[1]).lower()
    from .import_sources import _BOOKS
    names = {name.lower(): name for _, name, _ in _BOOKS}
    if short not in names:
        return None
    return f"{names[short]} {int(match[2])}:{int(match[3])}"


def _bible_range(value: str) -> tuple[str, int, int, int] | None:
    """Resolve bounded verse ranges and common names absent from verse text."""
    match = re.search(r"\b((?:[123]\s*)?(?:Мф|Мк|Лк|Ин|Деян|Иак|Пет|Иуд|Рим|Кор|Гал|Еф|Флп|Кол|Фес|Тим|Тит|Флм|Евр|Откр))\.?\s*(\d+)\s*:\s*(\d+)\s*[-\u2013\u2014]\s*(\d+)", value, re.IGNORECASE)
    if match:
        reference = _bible_reference(match[0])
        start, end = int(match[3]), int(match[4])
        if reference and 0 < start <= end and end - start < 40:
            book, address = reference.rsplit(" ", 1)
            return book, int(address.split(":")[0]), start, end
        return None
    if _bible_reference(value):
        return None  # Explicit verse addresses take priority over topic names.
    if re.search(r"\bпрощать\b", value, re.IGNORECASE) and re.search(
        r"\b(?:человек[а-яё]*|люд[а-яё]*)\b", value, re.IGNORECASE,
    ):
        return "Мф", 6, 12, 15
    topics = (
        (r"\bблудн[а-яё]*\s+сын[а-яё]*\b", ("Лк", 15, 11, 32)),
        (r"\bотче\s+наш\b", ("Мф", 6, 9, 13)),
        (r"\bне\s+судите\b", ("Мф", 7, 1, 5)),
        (r"\bмилосердн[а-яё]*\s+самарян[а-яё]*\b", ("Лк", 10, 25, 37)),
        (r"\b(?:мол[а-яё]*\s+вместе|совместн[а-яё]*\s+молитв[а-яё]*|соборн[а-яё]*\s+молитв[а-яё]*)\b", ("Мф", 18, 19, 20)),
    )
    for pattern, address in topics:
        if re.search(pattern, value, re.IGNORECASE):
            return address
    return None


def _document_topic(value: str) -> tuple[str, tuple[str, ...]] | None:
    """Operator-selected source locations for a narrow preparation question."""
    if _bible_reference(value):
        return None
    if re.search(r"\bподгот[а-яё]*\b", value, re.IGNORECASE) and re.search(
        r"\b(?:причаст[а-яё]*|причащ[а-яё]*|евхарист[а-яё]*)\b", value, re.IGNORECASE,
    ):
        return "eucharist-2015", (
            "II, абзац 1", "II, абзац 2", "II, абзац 5", "II, абзац 11",
            "III, абзац 1", "III, абзац 2",
        )
    if re.search(r"\b(?:зачем|для\s+чего)\b", value, re.IGNORECASE) and re.search(
        r"\b(?:исповед[а-яё]*|покаяни[а-яё]*)\b", value, re.IGNORECASE,
    ):
        return "filaret-catechism-2013", ("Вопрос 348", "Вопрос 349", "Вопрос 350", "Вопрос 351")
    return None


def _cosine(left, right) -> float:
    if left is None or right is None or len(left) != len(right):
        return 0.0
    norm = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(x*y for x, y in zip(left, right)) / norm if norm else 0.0
