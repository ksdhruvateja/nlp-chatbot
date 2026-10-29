"""Knowledge ingestion (prepared in advance, outside the request path).

Approved manuals and trusted repair guides
  -> check permissions, source and version
  -> extract text, tables and page references
  -> clean and split into passages
  -> generate embeddings
  -> PostgreSQL + pgvector and keyword (tsvector) index
"""
from __future__ import annotations

import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field

from pypdf import PdfReader

from . import db
from .embeddings import embed_many

ALLOWED_PERMISSIONS = {"approved", "licensed", "public_domain", "internal", "manufacturer_public"}
TARGET_WORDS = 180
OVERLAP_WORDS = 30


class IngestionRejected(ValueError):
    pass


@dataclass
class DocumentMeta:
    title: str
    source: str
    permission: str
    approved_by: str
    service_category: str
    version: str
    publisher: str = ""
    brand: str | None = None
    model_numbers: list[str] = field(default_factory=list)
    # Crawled documents arrive as "pending" and stay out of retrieval until an admin approves them.
    review_status: str = "approved"
    source_url: str | None = None
    doc_type: str | None = None
    published_year: int | None = None
    raw_storage_key: str | None = None


@dataclass
class Block:
    page: int | None
    section: str
    text: str
    is_table: bool = False


@dataclass
class Passage:
    ord: int
    page: int | None
    section: str
    content: str
    is_table: bool


# ------------------------------------------------------------ validation
def check_permissions(meta: DocumentMeta) -> None:
    problems = []
    if meta.permission not in ALLOWED_PERMISSIONS:
        problems.append(f"permission must be one of {sorted(ALLOWED_PERMISSIONS)}")
    for name in ("title", "source", "approved_by", "service_category", "version"):
        if not str(getattr(meta, name) or "").strip():
            problems.append(f"{name} is required")
    if problems:
        raise IngestionRejected("; ".join(problems))


# ------------------------------------------------------------ extraction
def _pages_from_pdf(data: bytes) -> list[tuple[int, str]]:
    reader = PdfReader(io.BytesIO(data))
    return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]


def _pages_from_text(text: str) -> list[tuple[int, str]]:
    # Form feeds or "--- page N ---" markers delimit pages in text exports.
    parts = re.split(r"\f|^-{3,}\s*page\s+\d+\s*-{3,}\s*$", text, flags=re.I | re.M)
    return [(i + 1, p) for i, p in enumerate(parts) if p.strip()]


def _strip_repeated_lines(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop running headers/footers that repeat on most pages."""
    if len(pages) < 3:
        return pages
    counts = Counter(line.strip() for _, t in pages for line in set(t.splitlines()) if line.strip())
    repeated = {line for line, n in counts.items() if n >= max(3, int(len(pages) * 0.6)) and len(line) < 80}
    return [(p, "\n".join(l for l in t.splitlines() if l.strip() not in repeated)) for p, t in pages]


_HEADING = re.compile(r"^(#{1,6}\s+.+|[A-Z0-9][A-Z0-9 /&,()-]{3,60}|\d+(\.\d+)*\s+[A-Z].{2,60})$")
_TABLE_ROW = re.compile(r"(\|.*\|)|(\S+(\t|\s{3,})\S+.*(\t|\s{3,})\S+)")


def extract_blocks(pages: list[tuple[int, str]]) -> list[Block]:
    blocks: list[Block] = []
    section = ""
    for page, text in _strip_repeated_lines(pages):
        para: list[str] = []
        table: list[str] = []

        def flush_para():
            if para:
                blocks.append(Block(page, section, " ".join(para)))
                para.clear()

        def flush_table():
            if table:
                if len(table) >= 2:
                    blocks.append(Block(page, section, "\n".join(table), is_table=True))
                else:
                    para.extend(table)
                table.clear()

        for raw in text.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                flush_table()
                flush_para()
                continue
            if _TABLE_ROW.search(line) and not stripped.startswith(("-", "*")):
                flush_para()
                if not re.fullmatch(r"[|\s:-]+", stripped):  # skip markdown separator rows
                    table.append(re.sub(r"\s{3,}|\t", " | ", stripped).strip("| "))
                continue
            flush_table()
            if _HEADING.match(stripped) and len(stripped.split()) <= 10:
                flush_para()
                section = stripped.lstrip("#").strip()
                continue
            # De-hyphenate words broken across lines.
            if para and para[-1].endswith("-") and stripped[:1].islower():
                para[-1] = para[-1][:-1] + stripped
            else:
                para.append(stripped)
        flush_table()
        flush_para()
    return blocks


def clean_text(text: str) -> str:
    text = text.replace("­", "").replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_passages(blocks: list[Block]) -> list[Passage]:
    passages: list[Passage] = []
    buf: list[str] = []
    buf_page: int | None = None
    buf_section = ""

    def emit():
        nonlocal buf
        if buf:
            passages.append(Passage(len(passages), buf_page, buf_section, clean_text(" ".join(buf)), False))
            buf = buf[-OVERLAP_WORDS:] if len(buf) > OVERLAP_WORDS * 2 else []

    for block in blocks:
        if block.is_table:
            emit()
            buf = []
            passages.append(Passage(len(passages), block.page, block.section, clean_text(block.text), True))
            continue
        if block.section != buf_section or block.page != buf_page:
            emit()
            buf = []
            buf_page, buf_section = block.page, block.section
        for word in block.text.split():
            buf.append(word)
            if len(buf) >= TARGET_WORDS:
                emit()
    emit()
    return [p for p in passages if len(p.content.split()) >= 5]


# ------------------------------------------------------------ pipeline
def ingest(data: bytes, filename: str, meta: DocumentMeta) -> dict:
    check_permissions(meta)
    checksum = hashlib.sha256(data).hexdigest()

    existing = db.fetch_one("SELECT id, checksum FROM knowledge_documents WHERE source = %s AND version = %s",
                            (meta.source, meta.version))
    if existing:
        if existing["checksum"] == checksum:
            return {"document_id": str(existing["id"]), "status": "unchanged", "passages": 0}
        raise IngestionRejected(f"{meta.source} version {meta.version} already ingested with different content; "
                                "bump the version")

    if filename.lower().endswith(".pdf") or data.startswith(b"%PDF"):
        pages = _pages_from_pdf(data)
    else:
        pages = _pages_from_text(data.decode("utf-8"))
    passages = split_passages(extract_blocks(pages))
    if not passages:
        raise IngestionRejected("no extractable text (scanned PDFs need OCR before ingestion)")

    models = [m.upper() for m in meta.model_numbers]
    vectors = embed_many([f"{p.section}\n{p.content}" for p in passages])

    approved = meta.review_status == "approved"
    with db.connection() as conn:
        if approved:  # a new approved version supersedes older versions of the same source
            conn.execute("UPDATE knowledge_documents SET active = false WHERE source = %s", (meta.source,))
        doc = conn.execute(
            """INSERT INTO knowledge_documents (title, source, publisher, permission, approved_by,
                   service_category, brand, model_numbers, version, checksum, active, review_status,
                   source_url, doc_type, published_year, raw_storage_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (meta.title, meta.source, meta.publisher, meta.permission, meta.approved_by,
             meta.service_category, meta.brand, models, meta.version, checksum, approved, meta.review_status,
             meta.source_url, meta.doc_type, meta.published_year, meta.raw_storage_key)).fetchone()
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO knowledge_passages (document_id, ord, page, section, content, is_table,
                       service_category, brand, model_numbers, version, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)""",
                [(doc["id"], p.ord, p.page, p.section, p.content, p.is_table, meta.service_category,
                  meta.brand, models, meta.version, db.vector_literal(v)) for p, v in zip(passages, vectors)])
    return {"document_id": str(doc["id"]), "status": "ingested", "passages": len(passages)}
