"""PDF text with page numbers (pypdf). Scanned PDFs have no text layer: v1 has no OCR."""

from __future__ import annotations

import logging
import math
import re
from io import BytesIO

from pypdf import PdfReader

from knowledge.extraction.model import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionError,
    ExtractionFailure,
)
from knowledge.extraction.normalise import clean_line, remove_repeated_lines, split_paragraphs

MIN_CHARS_PER_PAGE = 50  # below this on average: probably scanned

# pypdf logs warnings that can quote document text; keep them out of the Lambda's logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pdf(data: bytes) -> ExtractedDocument:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError(ExtractionFailure.EXTRACTION_FAILED, "PDF is encrypted")
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError(ExtractionFailure.EXTRACTION_FAILED, "unreadable PDF") from None

    if not page_texts:
        raise ExtractionError(ExtractionFailure.NO_EXTRACTABLE_TEXT, "PDF has no pages")

    pages = remove_repeated_lines([text.splitlines() for text in page_texts])
    text_chars = sum(len("".join(line.split())) for lines in pages for line in lines)
    if text_chars / len(pages) < MIN_CHARS_PER_PAGE:
        raise ExtractionError(
            ExtractionFailure.NO_EXTRACTABLE_TEXT,
            f"{text_chars / len(pages):.0f} characters per page on average (scanned?)",
        )

    return ExtractedDocument(blocks=tuple(_blocks(pages)), page_count=len(pages))


# --- headings -----------------------------------------------------------------------------
# PDFs don't mark headings, and pypdf returns plain lines. Without headings a short PDF
# becomes one chunk covering every topic, which matches each question only weakly. A line
# is taken as a heading when it looks like one *and* sits between sentences:
#   - short: at most 8 words, and under 70% of the document's typical full line (so a
#     narrow two-column layout doesn't turn every line into a heading), max 60 characters,
#   - starts with a capital letter,
#   - no closing punctuation, no table separators, not a list item,
#   - the previous line ended a sentence (or the document starts here, or it was a heading),
#   - the next line starts with a capital or digit (the section's first sentence).
# The last two conditions keep the tail of a wrapped sentence from passing as a heading.
MAX_HEADING_WORDS = 8
MAX_HEADING_CHARS = 60
HEADING_SHARE_OF_FULL_LINE = 0.7
_SENTENCE_END = re.compile(r"""[.!?:)"'\u201d\u2019]$""")
_NOT_HEADING_END = re.compile(r"[.,;:!?]$")
_LIST_START = re.compile(r"^(?:[-*+\u2022]|\d{1,3}[.)])\s")


def _looks_like_heading(line: str, max_chars: float) -> bool:
    return (
        0 < len(line) <= max_chars
        and len(line.split()) <= MAX_HEADING_WORDS
        and line[0].isupper()
        and not _NOT_HEADING_END.search(line)
        and "|" not in line
        and not _LIST_START.match(line)
        and any(c.isalpha() for c in line)
    )


def find_headings(lines: list[str]) -> set[int]:
    """Indexes of heading lines in the document's lines (all pages, in order)."""
    headings: set[int] = set()
    if not lines:
        return headings
    lengths = sorted(len(line) for line in lines)
    # The document's typical full line: the 90th-percentile length, rounded up so a short
    # document with a few lines still uses one of its longer lines.
    full_line = lengths[math.ceil(0.9 * (len(lengths) - 1))]
    max_chars = min(MAX_HEADING_CHARS, HEADING_SHARE_OF_FULL_LINE * full_line)
    for i, line in enumerate(lines):
        if not _looks_like_heading(line, max_chars):
            continue
        previous_ok = i == 0 or (i - 1) in headings or bool(_SENTENCE_END.search(lines[i - 1]))
        following = lines[i + 1] if i + 1 < len(lines) else ""
        next_ok = bool(following) and (following[0].isupper() or following[0].isdigit())
        if previous_ok and next_ok:
            headings.add(i)
    return headings


def _blocks(pages: list[list[str]]) -> list[Block]:
    """One block per run of lines sharing a heading and a page: sections split chunks,
    pages keep citations accurate (a section crossing a page break gives two blocks with
    the same heading, which the chunker keeps together)."""
    flat = [
        (number, clean)
        for number, lines in enumerate(pages, start=1)
        for line in lines
        if (clean := clean_line(line))
    ]
    headings = find_headings([text for _, text in flat])
    blocks: list[Block] = []
    heading: tuple[str, ...] = ()
    run: list[str] = []
    run_page: int | None = None

    def flush() -> None:
        if run and run_page is not None:
            for text, is_list in split_paragraphs("\n".join(run)):
                kind = BlockKind.LIST if is_list else BlockKind.PARAGRAPH
                blocks.append(Block(text=text, heading_path=heading, page=run_page, kind=kind))
        run.clear()

    for i, (page, text) in enumerate(flat):
        if i in headings:
            flush()
            heading = (text,)
            continue
        if page != run_page:
            flush()
            run_page = page
        run.append(text)
    flush()
    return blocks
