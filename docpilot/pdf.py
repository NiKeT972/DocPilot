"""PDF text cleaning, page-aware chunking and server-side parsing."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

MIN_PAGE_CHARS = 20

_HYPHEN_BREAK = re.compile(r"(\w)-\s*\n\s*(\w)")
_WS = re.compile(r"[ \t\f\v\r ]+")
_MULTI_NL = re.compile(r"\n{3,}")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class PageText:
    page: int  # 1-based
    text: str


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    doc_name: str
    page: int
    chunk_index: int
    content: str


def clean_text(text: str) -> str:
    """Join hyphenated line breaks, strip control chars and collapse whitespace."""
    if not text:
        return ""
    text = _CTRL.sub(" ", text)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = _MULTI_NL.sub("\n\n", text)
    # Single newlines inside a paragraph become spaces; blank lines stay paragraph breaks.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return _WS.sub(" ", text).strip()


def is_scanned(text: str) -> bool:
    """A page with (almost) no text layer is treated as scanned and needs OCR."""
    return len(clean_text(text)) < MIN_PAGE_CHARS


def make_splitter(chunk_size: int, chunk_overlap: int) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
    )


def chunk_pages(
    pages: list[PageText],
    doc_id: str,
    doc_name: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 150,
    start_index: int = 0,
) -> list[Chunk]:
    """Split each page independently so every chunk maps to exactly one page."""
    splitter = make_splitter(chunk_size, chunk_overlap)
    chunks: list[Chunk] = []
    idx = start_index
    for p in pages:
        text = clean_text(p.text)
        if len(text) < MIN_PAGE_CHARS:
            continue
        for piece in splitter.split_text(text):
            piece = piece.strip()
            if not piece:
                continue
            chunks.append(Chunk(doc_id, doc_name, p.page, idx, piece))
            idx += 1
    return chunks


def extract_pages(data: bytes) -> list[PageText]:
    """Server-side parse for `/api/upload` (small files only)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # malformed page: keep going
            text = ""
        pages.append(PageText(i, text))
    return pages
