"""Operator-reviewed corpus importer: preview -> inspect bundle -> apply hash.

python -m pastoral_bot.import_sources preview --version 2026-10-03 --output /tmp/corpus.json
python -m pastoral_bot.import_sources apply /tmp/corpus.json --approve SHA256
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup


class SourceImportError(RuntimeError):
    pass


def load_manifest(path: Path | None = None) -> dict[str, dict]:
    data = json.loads((path or Path(__file__).with_name("sources.json")).read_text(encoding="utf-8"))
    return {source["key"]: source for source in data["sources"]}


def bundle_hash(bundle: dict) -> str:
    return hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate_bundle(bundle: dict) -> None:
    manifest = load_manifest()
    if bundle.get("format") != 1 or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", bundle.get("version", "")):
        raise ValueError("invalid_bundle_version")
    documents = bundle.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("empty_bundle")
    seen = set()
    for document in documents:
        key = document.get("source_key")
        if key not in manifest or key in seen:
            raise ValueError("unknown_or_duplicate_source")
        seen.add(key)
        spec = manifest[key]
        for field in ("title", "edition", "canonical_url"):
            if document.get(field) != spec[field]:
                raise ValueError("source_metadata_mismatch")
        passages = document.get("passages", [])
        if len(passages) < spec["minimum_passages"]:
            raise ValueError(f"source_content_incomplete:{key}:{len(passages)}")
        locators = set()
        for passage in passages:
            locator, content, url = passage.get("locator"), passage.get("text"), passage.get("url")
            if not isinstance(locator, str) or not locator or locator in locators:
                raise ValueError("invalid_passage_locator")
            locators.add(locator)
            if not isinstance(content, str) or not content.strip() or len(content) > 2500:
                raise ValueError("invalid_passage_text")
            allowed = (spec["canonical_url"], spec["fetch_url"])
            if not isinstance(url, str) or not any(url == base or url.startswith(base + "#") or url.startswith(base + "?") or (
                spec["parser"] == "catechism_html" and url.startswith(base) and re.fullmatch(r"[1-5](?:#[A-Za-z0-9_]+)?", url[len(base):])
            ) for base in allowed):
                raise ValueError("invalid_passage_url")


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    """Fixed manifest URLs only; bounded body, no raw HTTP exceptions."""
    try:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise SourceImportError("source_http_error")
            parts, size = [], 0
            async for part in response.content.iter_chunked(65536):
                size += len(part)
                if size > 20_000_000:
                    raise SourceImportError("source_too_large")
                parts.append(part)
            return b"".join(parts)
    except SourceImportError:
        raise
    except Exception:
        raise SourceImportError("source_download_failed") from None


async def fetch_source(spec: dict) -> list[tuple[str, bytes]]:
    urls = [spec["fetch_url"]]
    if spec["parser"] == "catechism_html":
        urls = [spec["fetch_url"] + str(chapter) for chapter in range(1, 6)]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90), headers={"User-Agent": "PastoralCorpusImporter/1.0"}) as session:
        return [(url, await _download(session, url)) for url in urls]


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


_BOOKS = [
    ("Св. Евангелие от Матфея", "Мф", "Mt"),
    ("Св. Евангелие от Марка", "Мк", "Mk"),
    ("Св. Евангелие от Луки", "Лк", "Lk"),
    ("Св. Евангелие от Иоанна", "Ин", "Jn"),
    ("Деяния св. Апостолов", "Деян", "Act"),
    ("Послание Иакова", "Иак", "Jac"),
    ("Первое послание Петра", "1 Пет", "1Pet"),
    ("Второе послание Петра", "2 Пет", "2Pet"),
    ("Первое послание Иоанна", "1 Ин", "1Jn"),
    ("Второе послание Иоанна", "2 Ин", "2Jn"),
    ("Третье послание Иоанна", "3 Ин", "3Jn"),
    ("Послание Иуды", "Иуд", "Juda"),
    ("Послание к Римлянам", "Рим", "Rom"),
    ("Первое послание к Коринфянам", "1 Кор", "1Cor"),
    ("Второе послание к Коринфянам", "2 Кор", "2Cor"),
    ("Послание к Галатам", "Гал", "Gal"),
    ("Послание к Ефесянам", "Еф", "Eph"),
    ("Послание к Филиппийцам", "Флп", "Phil"),
    ("Послание к Колоссянам", "Кол", "Col"),
    ("Первое послание к Фессалоникийцам", "1 Фес", "1Thes"),
    ("Второе послание к Фессалоникийцам", "2 Фес", "2Thes"),
    ("Первое послание к Тимофею", "1 Тим", "1Tim"),
    ("Второе послание к Тимофею", "2 Тим", "2Tim"),
    ("Послание к Титу", "Тит", "Tit"),
    ("Послание к Филимону", "Флм", "Phlm"),
    ("Послание к Евреям", "Евр", "Hebr"),
    ("Откровение Иоанна Богослова", "Откр", "Apoc"),
]


def parse_nt_fb2(payload: bytes) -> list[dict]:
    # XML entity declarations are unnecessary and rejected, not resolved.
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise SourceImportError("unsafe_xml")
    root = ET.fromstring(payload)
    namespace = {"f": "http://www.gribuser.ru/xml/fictionbook/2.0"}
    known = {title: (short, code) for title, short, code in _BOOKS}
    seen, result = set(), []
    for book in root.findall("f:body/f:section", namespace):
        title_node = book.find("f:title", namespace)
        title = _clean("".join(title_node.itertext())) if title_node is not None else ""
        if title not in known:
            continue
        seen.add(title)
        short, code = known[title]
        chapters = book.findall("f:section", namespace)
        # Philemon, 2/3 John and Jude have one chapter and no nested section.
        for chapter in chapters or [book]:
            node = chapter.find("f:title", namespace)
            chapter_title = _clean("".join(node.itertext())) if node is not None else ""
            match = re.fullmatch(r"Глава\s+(\d+)", chapter_title)
            if not match and chapters:
                raise SourceImportError("unrecognized_bible_chapter")
            number = int(match[1]) if match else 1
            # Some actual verses are inside FB2 <cite> blocks, not direct p's.
            for paragraph in chapter.findall(".//f:p", namespace):
                verse_node = paragraph.find("f:sup", namespace)
                if verse_node is None:
                    continue
                verse = _clean("".join(verse_node.itertext()))
                if not verse.isdigit():
                    raise SourceImportError("unrecognized_bible_verse")
                entire = _clean("".join(paragraph.itertext()))
                content = entire[len(verse):].strip()
                result.append({"locator": f"{short} {number}:{verse}", "text": content, "url": f"https://azbyka.ru/biblia/?{code}.{number}:{verse}&r"})
    if seen != set(known):
        raise SourceImportError("new_testament_books_missing")
    return result


def _split_content(content: str, maximum: int = 2200) -> list[str]:
    chunks = []
    while len(content) > maximum:
        boundary = content.rfind(" ", 0, maximum)
        if boundary < maximum // 2:
            boundary = maximum
        chunks.append(content[:boundary].strip())
        content = content[boundary:].strip()
    if content:
        chunks.append(content)
    return chunks


def _append(result: list, locator: str, content: str, url: str) -> None:
    for number, chunk in enumerate(_split_content(content), 1):
        result.append({"locator": locator if number == 1 else f"{locator}, фрагмент {number}", "text": chunk, "url": url})


def parse_catechism_html(payload: bytes, url: str) -> list[dict]:
    soup = BeautifulSoup(payload, "html.parser")
    book = soup.select_one("div.book")
    if book is None:
        raise SourceImportError("catechism_content_missing")
    for node in book.select("svg, script, style, .bg_data_tooltip"):
        node.decompose()
    result, pieces = [], []
    question, anchor, question_anchor = "", "", ""

    def flush():
        if question and pieces:
            _append(result, question, _clean(" ".join(pieces)), url + ("#" + question_anchor if question_anchor else ""))

    for node in book.find_all(["a", "p", "h1", "h2", "h3", "h4", "h5", "h6"]):
        if node.name == "a":
            if node.get("id"):
                anchor = node["id"]
            continue
        content = _clean(node.get_text(" ", strip=True))
        if node.name.startswith("h"):
            flush()
            question, pieces = "", []
            continue
        match = re.match(r"^(\d+)\.\s+", content)
        if match and "h7" in node.get("class", []):
            flush()
            question, question_anchor, pieces = f"Вопрос {match[1]}", anchor, [content]
        elif question and content:
            pieces.append(content)
    flush()
    if not result:
        raise SourceImportError("catechism_questions_missing")
    return result


def parse_document_html(payload: bytes, *, parser: str, canonical_url: str) -> list[dict]:
    soup = BeautifulSoup(payload, "html.parser")
    selector = "main div.content" if parser == "social_html" else "div.detail_text"
    article = soup.select_one(selector)
    if article is None:
        raise SourceImportError("document_content_missing")
    for node in article.select("script, style, nav, .bg_data_tooltip"):
        node.decompose()
    result, section, paragraph = [], "Введение", 0
    for node in article.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]):
        if node.find(["p", "li"]) is not None:
            continue
        content = _clean(node.get_text(" ", strip=True))
        if not content:
            continue
        # The official Social Concept uses Cyrillic Х for chapter X (and its
        # subsections); the Eucharist page uses standalone <h2>III.</h2>.
        # Normalize only the numeric label, never the source's body text.
        label = re.match(r"^([IVXХІ]+)\s*\.\s*(?:(\d+)\s*\.)?(?=\s|$)", content)
        roman = label[1].translate(str.maketrans({"Х": "X", "І": "I"})) if label else ""
        if roman not in {"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI"}:
            label = None
        if label and label[2]:
            section = f"{roman}.{int(label[2])}"
            paragraph = 0
        elif label and len(content) < 250:
            suffix = content[label.end():].strip()
            section = roman
            paragraph = 0
            _append(result, f"Раздел {roman}" + (f". {suffix}" if suffix else ""), content, canonical_url)
            continue
        paragraph += 1
        _append(result, f"{section}, абзац {paragraph}", content, canonical_url)
    if not result:
        raise SourceImportError("document_paragraphs_missing")
    return result


def parse_source(spec: dict, payload: bytes, url: str | None = None) -> list[dict]:
    parser = spec["parser"]
    if parser == "nt_fb2":
        return parse_nt_fb2(payload)
    if parser == "catechism_html":
        return parse_catechism_html(payload, url or spec["fetch_url"])
    if parser in ("social_html", "eucharist_html"):
        return parse_document_html(payload, parser=parser, canonical_url=spec["canonical_url"])
    raise SourceImportError("unknown_source_parser")


async def preview_bundle(source_ids: list[str] | None, version: str) -> dict:
    manifest = load_manifest()
    documents = []
    for key in source_ids or manifest.keys():
        if key not in manifest:
            raise SourceImportError("unknown_source")
        spec = manifest[key]
        passages = []
        for url, payload in await fetch_source(spec):
            passages.extend(parse_source(spec, payload, url))
        documents.append({
            "source_key": key, "title": spec["title"], "edition": spec["edition"],
            "canonical_url": spec["canonical_url"], "passages": passages,
        })
    bundle = {"format": 1, "version": version, "documents": documents}
    validate_bundle(bundle)
    return bundle


async def _run(args) -> None:
    if args.action == "preview":
        bundle = await preview_bundle(args.source, args.version)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"path": str(args.output), "approve_hash": bundle_hash(bundle), "passages": {d["source_key"]: len(d["passages"]) for d in bundle["documents"]}}, ensure_ascii=False))
        return
    from .config import BotSettings
    from .knowledge import Knowledge
    from .storage import Store

    settings = BotSettings()
    if not settings.database_url:
        raise ValueError("Set PASTORAL_DATABASE_URL")
    store = Store(settings.database_url)
    try:
        await store.initialize()
        knowledge = Knowledge(store, settings)
        await knowledge.initialize()
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = await knowledge.import_documents(bundle, approve_hash=args.approve)
        print(json.dumps({"imported": result}, ensure_ascii=False))
    finally:
        await store.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import only operator-reviewed, versioned Orthodox sources")
    sub = parser.add_subparsers(dest="action", required=True)
    preview = sub.add_parser("preview")
    preview.add_argument("--source", action="append", choices=list(load_manifest()))
    preview.add_argument("--version", required=True)
    preview.add_argument("--output", type=Path, required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("bundle", type=Path)
    apply.add_argument("--approve", required=True, help="Exact SHA256 emitted by preview, after content review")
    try:
        asyncio.run(_run(parser.parse_args()))
    except (SourceImportError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
