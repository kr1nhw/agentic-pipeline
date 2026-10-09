"""Text operators: the free tier that should handle most pages.

These are deliberately boring. For a born-digital document the text layer *is*
the document — extracting it is not an approximation, it is ground truth — so
these operators run first, they cost nothing, and the vision path only exists for
the pages where they demonstrably fail.
"""

from __future__ import annotations

import re
from typing import Any

from docpipe.api.types import BBox, Block, Cost, OpContext, OpResult, Timer

WHITESPACE = re.compile(r"[ \t\u00a0]+")
BLANK_LINES = re.compile(r"\n{3,}")

#: Font sizes that stand out from the body become headings. The ratio is
#: relative to the modal size, so it survives different point scales.
HEADING_SIZE_RATIO = 1.12

#: Blocks shorter than this are never headings: a two-character line is a label.
MIN_HEADING_CHARS = 3


def _block(ctx: OpContext, block_type: str, text: str, **kwargs: Any) -> Block:
    order = len(ctx.state.record.blocks)
    return Block(
        block_id=f"{ctx.state.page_id}:b{order}",
        page_id=ctx.state.page_id,
        order=order,
        type=block_type,  # type: ignore[arg-type]
        text=text,
        source_op=kwargs.pop("source_op", "pdf_text_layer"),
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Text layer
# ─────────────────────────────────────────────────────────────────────────────


def op_pdf_text_layer(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Emit the page's own text layer as paragraph blocks, in reading order.

    The geometry path is the default, and it is the difference between usable and
    unusable output on a two-column paper. A flat text layer has no idea which
    column a line belongs to, so a top-to-bottom sort produces interleaved
    nonsense that still reads like prose. Grouping characters into lines with
    boxes lets the column gutter be measured, and the columns be read one after
    the other, which is what a person does.

    This is still *ground truth*: every character is the file's own. Only the
    order and the paragraph boundaries are inferred, and both are recorded as
    provenance on the blocks.
    """
    timer = Timer()
    text = ctx.state.record.text_layer
    if text is None and ctx.session is not None:
        text = ctx.session.page_text(ctx.state.page_no)
        ctx.state.record.text_layer = text
        ctx.state.record.text_layer_chars = len((text or "").strip())
    text = text or ""

    drop_running_heads = bool(params.get("drop_running_heads", True))
    # OPT-IN, and off by default. The character-box path recovers spaces and line
    # breaks from geometry, but a band of equal-y characters can contain both a
    # table caption and an adjacent body line whose x-ranges overlap — sorting
    # that band by x interleaves the two into gibberish. Doing this properly needs
    # real layout analysis (pdfminer's LTTextLine objects), not a hand-rolled band
    # sweep, so the flat text layer stays the default until that exists.
    # See docs/architecture.md § "Known gaps".
    use_geometry = bool(params.get("geometry", False)) and ctx.session is not None and hasattr(ctx.session, "page_lines")

    blocks: list[Block] = []
    ordering = "flat"
    if use_geometry:
        try:
            lines = ctx.session.page_lines(ctx.state.page_no)
        except Exception:  # noqa: BLE001 - fall back to the flat layer, never fail the page
            lines = []
        if lines:
            blocks, ordering = _blocks_from_lines(ctx, lines, drop_running_heads=drop_running_heads)

    if not blocks:
        paragraphs = [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]
        if len(paragraphs) <= 1:
            paragraphs = _rejoin_wrapped_lines(text)
        for paragraph in paragraphs:
            if drop_running_heads and _looks_like_running_head(paragraph):
                continue
            blocks.append(_block(ctx, "paragraph", paragraph, source_op="pdf_text_layer"))

    return OpResult(
        blocks=blocks,
        signals={"text_layer_chars": len(text.strip())},
        cost=Cost(duration_ms=timer.ms()),
        metadata={"ordering": ordering},
    )


def _blocks_from_lines(
    ctx: OpContext,
    lines: list[dict[str, Any]],
    *,
    drop_running_heads: bool,
) -> tuple[list[Block], str]:
    """Turn positioned lines into paragraph blocks, column by column."""
    usable = [line for line in lines if line["text"].strip()]
    if not usable:
        return [], "flat"

    page_width = ctx.state.record.signals.page_width
    if not page_width:
        page_width = max(line["x1"] for line in usable)

    spans = _detect_column_spans(usable, float(page_width))
    ordering = "single_column" if len(spans) == 1 else f"columns:{len(spans)}"

    blocks: list[Block] = []
    for start, end in spans:
        column = sorted(
            [line for line in usable if start <= (line["x0"] + line["x1"]) / 2 <= end],
            key=lambda line: line["top"],
        )
        blocks.extend(_paragraphs_from_column(ctx, column, drop_running_heads=drop_running_heads))
    return blocks, ordering


def _detect_column_spans(lines: list[dict[str, Any]], page_width: float) -> list[tuple[float, float]]:
    """Find vertical bands of text, splitting on a real gutter.

    The gutter must be *empty*, not merely narrow: papers set columns close
    together, and splitting on the widest gap without checking that no line
    crosses it shreds a single-column page into two.
    """
    if page_width <= 0 or len(lines) < 6:
        return [(0.0, page_width)]

    bins = 200
    occupied = [False] * bins
    for line in lines:
        first = max(0, min(bins - 1, int(line["x0"] / page_width * bins)))
        last = max(0, min(bins - 1, int(line["x1"] / page_width * bins)))
        for index in range(first, last + 1):
            occupied[index] = True

    # Look for the widest empty run strictly inside the page. Requirements: it is
    # wide enough to be a gutter, and both sides carry a real share of the lines.
    best: tuple[int, int] | None = None
    run_start: int | None = None
    for index in range(int(bins * 0.15), int(bins * 0.85)):
        if not occupied[index]:
            run_start = index if run_start is None else run_start
            continue
        if run_start is not None:
            if best is None or (index - run_start) > (best[1] - best[0]):
                best = (run_start, index)
            run_start = None
    if best is None or (best[1] - best[0]) < bins * 0.02:
        return [(0.0, page_width)]

    gutter_start = best[0] / bins * page_width
    gutter_end = best[1] / bins * page_width
    left = [line for line in lines if line["x1"] <= gutter_start]
    right = [line for line in lines if line["x0"] >= gutter_end]
    if len(left) < 3 or len(right) < 3:
        return [(0.0, page_width)]
    # A full-width element (title, wide figure) means the page is not columnar.
    if len(left) + len(right) < len(lines) * 0.8:
        return [(0.0, page_width)]
    return [(0.0, gutter_start), (gutter_end, page_width)]


def _paragraphs_from_column(
    ctx: OpContext,
    lines: list[dict[str, Any]],
    *,
    drop_running_heads: bool,
) -> list[Block]:
    """Merge wrapped lines into paragraphs inside one column."""
    if not lines:
        return []
    heights = sorted(line["size"] for line in lines)
    median_height = heights[len(heights) // 2] or 10.0
    widths = sorted(line["x1"] - line["x0"] for line in lines)
    column_width = widths[int(len(widths) * 0.9)] or 1.0

    blocks: list[Block] = []
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        if not buffer:
            return
        text = " ".join(line["text"] for line in buffer).strip()
        bbox = BBox(
            x0=min(line["x0"] for line in buffer),
            y0=min(line["top"] for line in buffer),
            x1=max(line["x1"] for line in buffer),
            y1=max(line["bottom"] for line in buffer),
        )
        lines_used = len(buffer)
        buffer.clear()
        if not text or (drop_running_heads and _looks_like_running_head(text)):
            return
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:t{len(blocks)}",
                page_id=ctx.state.page_id,
                order=len(blocks),
                type="paragraph",
                text=text,
                bbox=bbox,
                source_op="pdf_text_layer",
                checks={"wrapped_lines": lines_used, "geometry": True},
            )
        )

    previous: dict[str, Any] | None = None
    for line in lines:
        if previous is not None:
            gap = line["top"] - previous["bottom"]
            short_previous = (previous["x1"] - previous["x0"]) < column_width * 0.72
            sentence_end = previous["text"].rstrip().endswith((".", "。", "!", "?", ":", "！", "？", "："))
            if gap > median_height * 1.35 or (short_previous and gap > median_height * 0.35) or (sentence_end and gap > median_height * 1.1):
                flush()
        buffer.append(line)
        previous = line
    flush()
    return blocks


def _rejoin_wrapped_lines(text: str) -> list[str]:
    """Join hard-wrapped lines into paragraphs, on sentence-ish boundaries."""
    paragraphs: list[str] = []
    buffer: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        buffer.append(line)
        # A line ending in sentence punctuation plus a following blank is the
        # only reliable paragraph signal in layout-mode output.
        if line.endswith((".", "。", "！", "？", ":", "：")) and len(" ".join(buffer)) > 200:
            paragraphs.append(" ".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer))
    return paragraphs


def _looks_like_running_head(text: str) -> bool:
    """A short, number-heavy line at the very start is usually a page header."""
    if len(text) > 90 or len(text) < 2:
        return False
    digits = sum(char.isdigit() for char in text)
    return digits / len(text) > 0.3 and len(text.split()) <= 4


def op_text_passthrough(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Emit the unit's text as-is.

    Used for Markdown, plain text and code units, where the ingest session has
    already produced the exact block structure the format implies. Passing it
    through unchanged is the honest operator: reflowing Markdown here would
    destroy the structure the format was chosen for.
    """
    timer = Timer()
    text = ctx.state.record.text_layer or ""
    heading = (ctx.state.record.signals.model_extra or {}).get("heading")
    heading_path = (ctx.state.record.signals.model_extra or {}).get("heading_path") or []
    layout = ctx.state.record.signals.layout_hint

    blocks: list[Block] = []
    if layout == "markdown":
        blocks.extend(_markdown_blocks(ctx, text, heading_path))
    elif layout == "code":
        blocks.append(_block(ctx, "code", text, source_op="text_passthrough", checks={"language": _language(ctx)}))
    else:
        for chunk in [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()] or ([text.strip()] if text.strip() else []):
            blocks.append(_block(ctx, "paragraph", chunk, source_op="text_passthrough"))
    if heading and layout != "markdown":
        blocks.insert(0, _block(ctx, "heading", heading, level=1, source_op="text_passthrough"))
    return OpResult(blocks=blocks, cost=Cost(duration_ms=timer.ms()))


def _language(ctx: OpContext) -> str:
    if ctx.session is not None and hasattr(ctx.session, "language"):
        return str(ctx.session.language)
    return "text"


def _markdown_blocks(ctx: OpContext, text: str, heading_path: list[str]) -> list[Block]:
    """Split a Markdown unit into headings, paragraphs and fenced code."""
    blocks: list[Block] = []
    fence = False
    buffer: list[str] = []
    fence_lang = ""

    def flush(kind: str, language: str = "") -> None:
        body = "\n".join(buffer).strip("\n")
        buffer.clear()
        if body.strip():
            checks = {"language": language} if language else {}
            blocks.append(_block(ctx, kind, body.strip(), source_op="text_passthrough", checks=checks))

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            if fence:
                flush("code", fence_lang)
                fence = False
                fence_lang = ""
            else:
                flush("paragraph")
                fence = True
                fence_lang = line.strip().strip("`").strip()
            continue
        if fence:
            buffer.append(line)
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if match:
            flush("paragraph")
            blocks.append(
                _block(
                    ctx,
                    "heading",
                    match.group(2).strip(),
                    level=len(match.group(1)),
                    source_op="text_passthrough",
                    checks={"heading_path": heading_path} if heading_path else {},
                )
            )
            continue
        if not line.strip():
            flush("paragraph")
            continue
        buffer.append(line)
    flush("code" if fence else "paragraph", fence_lang)
    return blocks


def op_normalize_whitespace(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Collapse runs of spaces and blank lines inside every block so far.

    A post-processing operator, not an extractor: it touches blocks already on
    the page and returns none of its own.
    """
    timer = Timer()
    changed = 0
    for block in ctx.state.record.blocks:
        if not block.text:
            continue
        cleaned = WHITESPACE.sub(" ", block.text)
        cleaned = BLANK_LINES.sub("\n\n", cleaned).strip()
        if cleaned != block.text:
            block.text = cleaned
            changed += 1
    return OpResult(
        cost=Cost(duration_ms=timer.ms()),
        metadata={"blocks_normalized": changed},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Headings from typography
# ─────────────────────────────────────────────────────────────────────────────


def op_pdf_font_heading_map(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Promote oversized lines to headings using the PDF's own font sizes.

    The heuristic is relative, not absolute: a line is a heading when its font is
    meaningfully larger than the page's modal body size, which survives papers,
    slides and reports alike without per-document tuning.
    """
    timer = Timer()
    if ctx.session is None or not hasattr(ctx.session, "path"):
        return OpResult(cost=Cost(duration_ms=timer.ms()), diagnostics=["no session for font analysis"])

    min_ratio = float(params.get("size_ratio", HEADING_SIZE_RATIO))
    max_levels = int(params.get("max_levels", 4))

    # A page whose largest text is its body text has no typographic heading, and
    # PDFium can establish that in milliseconds. The threshold here is looser
    # than `min_ratio`, so this only ever skips pages that could not have
    # produced a heading anyway.
    if ctx.session is not None and hasattr(ctx.session, "page_profile"):
        profile = ctx.session.page_profile(ctx.state.page_no)
        if profile.get("profiled") and not profile.get("has_oversized_text", True):
            return OpResult(
                cost=Cost(duration_ms=timer.ms()),
                metadata={"skipped": "uniform text size", "median_height": profile.get("median_height")},
            )

    # Default backend is pdfminer through pdfplumber, which is slower but gets
    # line separation right on dense layouts.
    #
    # `backend: "pdfium"` reads font sizes directly from PDFium — about ten times
    # faster, and its size histogram is cleaner (title 17.2, heading 12, body 9 as
    # distinct buckets rather than box heights that vary with the glyph). It is
    # NOT the default because its line assembly is not yet reliable on pages where
    # two text runs share a baseline band: an A/B against this backend showed 0%
    # agreement, with authors interleaved into affiliations
    # (`Niv DayanP,l Mioposshe Twitto`) and phantom 140pt "headings". Making it
    # correct needs the run separation in `docpipe.ingest.pdf` finished, not
    # another threshold. Exposed so that work is measurable rather than blind.
    backend = str(params.get("backend", "pdfminer"))

    # A wall-clock budget, because cost here is not predictable from anything
    # cheap. Measured on a harvested ACL paper: one page carries a scatter plot of
    # ~73,000 decomposed vector objects and pdfminer needs 8.7 s/page on that
    # document against 0.1 s/page on a comparable paper. PDFium's own object count
    # (118 paths, 750 segments) does not predict it, and neither does a ruling-line
    # count — the explosion only appears once pdfminer has parsed the page.
    #
    # So the page is allowed a bounded amount of time and no more. Exceeding it is
    # not a failure: it is evidence that this page needs a different route, and the
    # gate escalates it to the vision path rather than the run stalling on it.
    budget_s = float(params.get("time_budget_s", getattr(ctx.config, "op_time_budget_s", 6.0)))
    try:
        if backend == "pdfium" and hasattr(ctx.session, "page_lines"):
            lines = ctx.session.page_lines(ctx.state.page_no)
        else:
            backend = "pdfminer"
            page = ctx.session.plumber_page(ctx.state.page_no) if hasattr(ctx.session, "plumber_page") else None
            if page is None:
                return OpResult()
            lines = _with_deadline(
                lambda: _group_chars_into_lines(page.chars),
                budget_s,
                op="pdf_font_heading_map",
                page_id=ctx.state.page_id,
            )
            if lines is None:
                if hasattr(ctx.session, "mark_pdfminer_timeout"):
                    ctx.session.mark_pdfminer_timeout(ctx.state.page_no)
                return OpResult(
                    cost=Cost(duration_ms=timer.ms()),
                    diagnostics=[
                        f"heading detection exceeded its {budget_s:.0f}s budget on this page; "
                        "the page needs a different route"
                    ],
                    metadata={"timeout": True, "budget_s": budget_s},
                )
    except Exception as exc:  # noqa: BLE001 - typography is an enhancement, not a requirement
        return OpResult(diagnostics=[f"font analysis failed: {type(exc).__name__}: {exc}"])

    if not lines:
        return OpResult(cost=Cost(duration_ms=timer.ms()), metadata={"backend": backend})

    sizes = [line["size"] for line in lines if line["size"] > 0]
    if not sizes:
        return OpResult(cost=Cost(duration_ms=timer.ms()))
    body_size = _modal(sizes)
    distinct = sorted({round(size, 1) for size in sizes if size >= body_size * min_ratio}, reverse=True)
    level_by_size = {size: min(index + 1, max_levels) for index, size in enumerate(distinct)}

    blocks: list[Block] = []
    for line in lines:
        text = line["text"].strip()
        if len(text) < MIN_HEADING_CHARS:
            continue
        key = round(line["size"], 1)
        level = level_by_size.get(key)
        if level is None:
            continue
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:h{len(blocks)}",
                page_id=ctx.state.page_id,
                order=len(blocks),
                type="heading",
                text=text,
                level=level,
                bbox=BBox(x0=line["x0"], y0=line["top"], x1=line["x1"], y1=line["bottom"]),
                source_op="pdf_font_heading_map",
                checks={"font_size": line["size"], "body_size": body_size, "backend": backend},
            )
        )

    # A heading that duplicates a paragraph on the same page is the same content
    # twice; drop the paragraph rather than emit both.
    if blocks and bool(params.get("replace_duplicates", True)):
        heading_texts = {b.text for b in blocks}
        remaining = [b for b in ctx.state.record.blocks if b.text.strip() not in heading_texts]
        ctx.state.record.blocks = remaining

    return OpResult(blocks=blocks, cost=Cost(duration_ms=timer.ms()), signals={"body_font_size": body_size})


def _group_chars_into_lines(chars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group pdfplumber character objects into visual lines."""
    if not chars:
        return []
    ordered = sorted(chars, key=lambda c: (round(float(c["top"]), 1), float(c["x0"])))
    lines: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_top: float | None = None
    for char in ordered:
        top = float(char["top"])
        if current_top is None or abs(top - current_top) <= 2.0:
            current.append(char)
            current_top = top if current_top is None else (current_top + top) / 2
        else:
            lines.append(_line_from_chars(current))
            current = [char]
            current_top = top
    if current:
        lines.append(_line_from_chars(current))
    return lines


def _line_from_chars(chars: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble one line, reconstructing the spaces pdfplumber omits.

    pdfplumber exposes words, not whitespace, so joining characters naively
    produced headings like ``3PROMISE:LSM-TREE&CUCKOOFILTER``. The gap between two
    characters is measured against the width of the characters on that same line,
    so one threshold works for a footnote and a title on the same page.
    """
    sizes = [float(c["size"]) for c in chars if c.get("size")]
    ordered = sorted(chars, key=lambda c: float(c["x0"]))
    sizes = [float(c["size"]) for c in ordered if c.get("size")]
    typical_size = sorted(sizes)[len(sizes) // 2] if sizes else 10.0
    # Measured on this corpus: intra-word gaps are ~0.0pt and a word space is
    # 4.3pt at a 17.2pt font. So the threshold scales with the font — 0.2 em —
    # rather than with the glyph advance, which misclassified a title's spaces.
    gap_threshold = max(1.0, typical_size * 0.2)
    pieces: list[str] = []
    previous_x1: float | None = None
    for char in ordered:
        if previous_x1 is not None and float(char["x0"]) - previous_x1 > gap_threshold:
            pieces.append(" ")
        pieces.append(str(char.get("text", "")))
        previous_x1 = float(char["x1"])
    text = "".join(pieces).strip()
    return {
        "text": text,
        "size": max(sizes) if sizes else 0.0,
        "x0": min(float(c["x0"]) for c in chars),
        "x1": max(float(c["x1"]) for c in chars),
        "top": min(float(c["top"]) for c in chars),
        "bottom": max(float(c["bottom"]) for c in chars),
    }


def _modal(values: list[float]) -> float:
    """The most common font size, rounded to a tenth of a point."""
    counts: dict[float, int] = {}
    for value in values:
        key = round(value, 1)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]

#: Timeouts already spent, per document, so one pathological document cannot leak
#: unbounded worker threads.
_TIMEOUTS: dict[str, int] = {}
TIMEOUTS_PER_DOCUMENT = 8


def _with_deadline(work: Any, seconds: float, *, op: str, page_id: str) -> Any | None:
    """Run `work` with a wall-clock deadline; return None if it is exceeded.

    Python cannot kill a thread, so the abandoned call keeps running until it
    finishes. That is a deliberate, bounded trade: the alternative is a corpus run
    that stalls for minutes on one page, and the leaked work is capped per
    document. When the cap is reached the deadline is not offered again — the
    document has declared itself pathological and the remaining pages go straight
    to the route the gate chooses.
    """
    if seconds <= 0:
        return work()
    document = page_id.split(":", 1)[0]
    if _TIMEOUTS.get(document, 0) >= TIMEOUTS_PER_DOCUMENT:
        return None
    import concurrent.futures

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"deadline-{op}")
    future = pool.submit(work)
    try:
        return future.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        _TIMEOUTS[document] = _TIMEOUTS.get(document, 0) + 1
        return None
    except Exception:  # noqa: BLE001 - a failed op is reported by the caller
        raise
    finally:
        # `wait=False` is the entire point. Using the executor as a context manager
        # calls shutdown(wait=True), which blocks until the abandoned call finishes
        # — so the deadline expired, the timeout was recorded, and the run waited
        # for the very work it had just given up on. Measured: no improvement at
        # all (138.22s vs 138.94s) until this became explicit.
        pool.shutdown(wait=False)

