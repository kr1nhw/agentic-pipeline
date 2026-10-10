"""Read a page image with a local OCR engine, at no token cost.

This exists for the case where there is nothing else to read: a page that is an
image has no characters for `pdftotext` to return and no fonts for the trust rule to
judge, so without a local OCR the only route for it is the vision model at ~4 100
tokens a page — a per-page cost that never falls with scale.

What it is NOT for, and this was measured rather than assumed: a scanned book that
already carries an OCR layer is not improved by OCRing it again. On page 300 of the
1789 Statutes at Large the layer reads `Ses s . I. Ch . 24.` and a fresh tesseract
pass reads `Sess. I. Cu. 24.` — 846 tokens against 844, identical word shape, and
neither is right: the layer inserts spaces, tesseract substitutes characters. Use
this for pages with no layer, not as a second opinion on a layer that exists.

Tesseract is installed in the image for exactly this. It is not a better reader than
the model on a hard page; it is a reader that costs nothing, so it goes first and
the model is kept for the pages it cannot manage.

The operator reports what it actually produced (`ocr_chars`, `ocr_lines`) and fails
cleanly when the engine is absent, so the escalation ladder can fall through to the
next rung instead of pretending the page was read.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from docpipe.api.types import Block, Cost, OpContext, OpResult

__all__ = ["OCR_BINARY", "op_page_ocr"]

OCR_BINARY = "tesseract"

#: A page of OCR is seconds of work, not minutes. The deadline is generous because
#: the rung exists to be cheap, and a page that cannot be read in this long is a page
#: the model should have.
OCR_TIMEOUT_S = 45.0


def ocr_available() -> str | None:
    """The OCR binary, or None. Used by the ladder to decide whether to offer it."""
    return shutil.which(OCR_BINARY)


def _paragraphs(text: str) -> list[str]:
    """Split OCR output into blocks the way a reader sees paragraphs.

    Tesseract emits one line per text line with no blank-line structure, so
    paragraphs have to come from somewhere: a blank line when the engine provides
    one, and otherwise a run of lines, because emitting a block per line makes the
    assembly stage treat each line as a paragraph and the chunker then splits
    mid-sentence.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        # A line ending in a hyphen broke a word across lines; rejoin it.
        if current and current[-1].endswith("-"):
            current[-1] = current[-1][:-1] + stripped
        else:
            current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    return [block for block in blocks if block.strip()]


def op_page_ocr(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """OCR this unit's raster and emit its text as paragraph blocks."""
    from docpipe.ops.text_ops import Timer, _with_deadline

    timer = Timer()
    binary = ocr_available()
    if binary is None:
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[f"{OCR_BINARY} is not installed in this image"],
            metadata={"skipped": "no ocr engine"},
        )

    language = str(params.get("language", "eng"))
    dpi = int(params.get("dpi", 200))
    page_size = str(params.get("psm", "3"))

    try:
        raster = ctx.raster(page_no=ctx.state.page_no, dpi=dpi, force=bool(params.get("force", False)))
    except Exception as exc:  # noqa: BLE001 - no raster means no OCR, not a failed page
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[f"cannot rasterise the page for OCR: {exc}"],
        )

    timeout = float(params.get("timeout_s", OCR_TIMEOUT_S))

    def read() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [binary, str(raster), "stdout", "-l", language, "--psm", page_size],
            capture_output=True,
            timeout=max(5.0, timeout),
            check=False,
        )

    try:
        completed = _with_deadline(read, timeout, op="page_ocr", page_id=ctx.state.page_id)
    except subprocess.SubprocessError as exc:
        return OpResult(ok=False, cost=Cost(duration_ms=timer.ms()), diagnostics=[f"ocr failed: {exc}"])

    if completed is None:
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[f"ocr exceeded its {timeout:.0f}s budget on this page"],
            metadata={"skipped": "ocr timeout"},
        )

    text = (completed.stdout or b"").decode("utf-8", "replace")
    paragraphs = _paragraphs(text)
    blocks = [
        Block(
            block_id=f"{ctx.state.page_id}:ocr{index}",
            page_id=ctx.state.page_id,
            order=index,
            type="paragraph",
            text=paragraph,
            source_op="page_ocr",
            confidence=0.6,
            checks={"engine": OCR_BINARY, "paragraphs": len(paragraphs)},
        )
        for index, paragraph in enumerate(paragraphs)
    ]
    characters = sum(len(block.text) for block in blocks)
    characters_total = len(text.strip())
    if not blocks:
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            signals={"ocr_chars": 0},
            diagnostics=["ocr produced no text on this page"],
            metadata={"ocr": binary},
        )
    return OpResult(
        blocks=blocks,
        signals={"ocr_chars": characters_total, "ocr_paragraphs": len(blocks)},
        cost=Cost(duration_ms=timer.ms()),
        # The characters are read from the pixels, so whatever the layer said is
        # superseded — the same reason a vision transcription replaces the layer.
        replace_blocks=bool(params.get("replace", True)),
        metadata={"ocr": binary, "dpi": dpi},
    )
