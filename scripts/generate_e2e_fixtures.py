"""Generate the synthetic browser-E2E resume fixtures (FIN-010).

Why a generator instead of checking in opaque binaries
------------------------------------------------------
FIN-010 requires a *non-seed* browser flow: upload a repository-bundled
synthetic PDF/DOCX and let the worker parse it. Shipping the binaries alone
would leave their contents unauditable and unreproducible, so this script is
the source of truth and the two files under ``apps/web/e2e/fixtures/`` are its
build output.

The content is deliberately pinned to strings the existing specs assert on:

* ``document-review.spec.ts`` selects the substring ``"Python 后端"`` inside a
  block that contains ``"6 年 Python"`` and pins it as evidence, and fills the
  name/year fields with ``张伟`` / ``6``. Both files must keep that phrasing.
* ``recruitment-flow.spec.ts`` relies on the *seeded* pool, so these fixtures
  add candidates rather than replacing any.

Both formats carry the same five logical blocks so PDF and DOCX parse to
comparable text; the extractor is format-agnostic by design.

Usage::

    python scripts/generate_e2e_fixtures.py            # write both fixtures
    python scripts/generate_e2e_fixtures.py --check    # fail if they drifted

``--check`` is the drift guard: it parses both fixtures and asserts they expose
the expected blocks, so a hand-edited fixture cannot silently diverge from this
script. The check compares *parsed content* rather than raw bytes -- DOCX is a
ZIP and PDF carries timestamps/IDs, so both formats legitimately differ
byte-for-byte on every regeneration even when the text is identical.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf
from docx import Document

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "apps" / "web" / "e2e" / "fixtures"
DOCX_NAME = "e2e-candidate-resume.docx"
PDF_NAME = "e2e-candidate-resume.pdf"

# The five blocks both formats must expose. Kept as plain strings so the DOCX
# paragraphs and the PDF lines are guaranteed to carry identical text.
BLOCKS: tuple[str, ...] = (
    "张伟",
    "6 年 Python 后端开发经验",
    "工作经历：2021-至今 某电商 高级后端工程师",
    "技能：Python、FastAPI、PostgreSQL",
    "教育背景：本科 计算机科学与技术",
)


def _docx_bytes() -> bytes:
    """Render the blocks as DOCX paragraphs, one block per paragraph.

    Character offsets are what the review screen records, so the paragraph must
    contain the exact substring the spec selects (``Python 后端`` inside the
    ``6 年 Python 后端开发经验`` block) rather than splitting on the skill.
    """
    document = Document()
    for block in BLOCKS:
        document.add_paragraph(block)
    from io import BytesIO

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _pdf_bytes() -> bytes:
    """Render the same blocks as one PDF page, one text line per block.

    CJK needs an explicit font. ``china-s`` is a built-in PyMuPDF font
    (Droid Sans Fallback) so no font file has to be shipped; passing it by
    ``fontname`` embeds the subset and the text round-trips through
    ``get_text()``, which is what the backend parser relies on.
    """
    document = pymupdf.open()
    page = document.new_page()
    y = 72.0
    for block in BLOCKS:
        page.insert_text((72, y), block, fontsize=12, fontname="china-s")
        y += 20.0
    payload = document.tobytes()
    document.close()
    return payload


def _build() -> dict[Path, bytes]:
    return {
        FIXTURE_DIR / DOCX_NAME: _docx_bytes(),
        FIXTURE_DIR / PDF_NAME: _pdf_bytes(),
    }


def _write() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for path, payload in _build().items():
        path.write_bytes(payload)
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(payload)} bytes)")


def _extract_blocks(path: Path) -> list[str]:
    """Return the non-empty text blocks the backend parser would see.

    Imported lazily so ``--check`` and the writer share one definition of
    "what the fixture contains" -- the real parser, not a reimplementation.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from backend.app.documents.parsers import ParseLimits, parser_for_media_type
    from backend.app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE

    media_type = {".pdf": PDF_MEDIA_TYPE, ".docx": DOCX_MEDIA_TYPE}[path.suffix]
    limits = ParseLimits(max_pdf_pages=10, max_extracted_chars=200_000, timeout_seconds=30)
    with path.open("rb") as handle:
        parsed = parser_for_media_type(media_type).parse(handle, limits)
    return [block.text.strip() for block in parsed.blocks if block.text.strip()]


def _check() -> int:
    """Return 0 when both fixtures parse to the expected blocks, 1 otherwise."""
    expected = [block for block in BLOCKS if block.strip()]
    drifted: list[str] = []
    for path in (FIXTURE_DIR / DOCX_NAME, FIXTURE_DIR / PDF_NAME):
        if not path.is_file():
            drifted.append(f"{path.relative_to(REPO_ROOT)} is missing")
            continue
        actual = _extract_blocks(path)
        if actual != expected:
            drifted.append(
                f"{path.relative_to(REPO_ROOT)} parses to {actual!r}, expected {expected!r}"
            )
    if drifted:
        for line in drifted:
            print(f"DRIFT: {line}", file=sys.stderr)
        print(
            "Regenerate with: python scripts/generate_e2e_fixtures.py",
            file=sys.stderr,
        )
        return 1
    print("E2E fixtures parse to the expected blocks.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed fixtures match this generator instead of writing",
    )
    arguments = parser.parse_args()
    if arguments.check:
        return _check()
    _write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
