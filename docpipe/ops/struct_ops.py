"""Structure operators: tables, reading order, captions.

Tables are the reason the vision path exists at all. ``pdfplumber`` recovers a
table from the ruling lines and text alignment that a born-digital PDF actually
contains, and it is right far more often than a model reading a picture — but it
returns *nothing* when the table has no rules and the columns are only implied by
whitespace. That failure is detectable, and detectable failure is what the gate
escalates on.
"""

from __future__ import annotations

import re
from typing import Any

from docpipe.api.types import BBox, Block, Cost, OpContext, OpResult, TablePayload, Timer

#: Rows that are entirely empty beyond this fraction mean the extraction failed.
EMPTY_CELL_LIMIT = 0.6

#: Above this many vector objects, a page is a figure rather than a table grid and
#: table detection is both meaningless and ruinously slow.
#:
#: Measured on a harvested ACL paper: `find_tables` costs <=0.07s on every page up
#: to 178 vectors, and 3.17s on the one page carrying 73,202 — a scatter plot drawn
#: as thousands of marks, which pdfminer decomposes into one line object each. That
#: single document took 149s for 16 pages, 3% of a 32-document corpus's entire
#: runtime. Note that PDFium reports only 118 *page objects* for that page, so the
#: explosion is invisible until pdfminer has parsed it — which is why the guard
#: belongs here, next to the parse, and not in the cheap page profile.
VECTOR_OBJECT_LIMIT = 20_000

#: A table smaller than this is usually a layout artefact, not a table.
MIN_TABLE_ROWS = 2
MIN_TABLE_COLS = 2

CAPTION_PATTERN = re.compile(
    r"^\s*(?:(?:table|figure|fig\.?|chart|algorithm|listing|图|表|附件)\s*[\d一二三四五六七八九十]+[.:：、]?)\s*(.*)$",
    re.IGNORECASE,
)


def op_pdf_tables(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Extract ruled tables with pdfplumber, falling back to text alignment.

    ``strategy`` chooses how cell boundaries are found:

    * ``lines`` — trust the ruling lines. Precise, and returns nothing when a
      table has none.
    * ``text`` — infer columns from whitespace. Recovers borderless tables, at
      the cost of occasionally splitting a sentence into columns.
    """
    timer = Timer()
    if ctx.session is None or not hasattr(ctx.session, "path"):
        return OpResult(cost=Cost(duration_ms=timer.ms()), diagnostics=["no session for table extraction"])

    strategy = str(params.get("strategy", "lines"))
    min_rows = int(params.get("min_rows", MIN_TABLE_ROWS))
    min_cols = int(params.get("min_cols", MIN_TABLE_COLS))
    max_empty = float(params.get("max_empty_frac", EMPTY_CELL_LIMIT))

    # Skip only when the page has NO vector paths at all. The lines strategy
    # derives its rules from vector objects, so with none there is nothing to
    # derive — that direction is safe by construction.
    #
    # A sharper-looking rule was tried first and measured: "skip unless the page
    # has at least two wide, thin paths". It dropped real tables on 4 of 32
    # sampled pages, because pdfminer also builds rules from filled rectangles and
    # from vertical strokes that a width-and-thickness test does not recognise. A
    # heuristic that silently loses tables is far worse than one that occasionally
    # runs an operator for nothing, so the rule is now the one that cannot be wrong.
    # Do not repeat a parse that has already been given up on.
    if ctx.session is not None and hasattr(ctx.session, "pdfminer_timed_out"):
        if ctx.session.pdfminer_timed_out(ctx.state.page_no):
            return OpResult(
                cost=Cost(duration_ms=timer.ms()),
                signals={"tables_found": 0},
                diagnostics=["table detection skipped: pdfminer already exceeded its budget on this page"],
                metadata={"skipped": "pdfminer timeout"},
            )

    if strategy == "lines" and ctx.session is not None and hasattr(ctx.session, "page_profile"):
        profile = ctx.session.page_profile(ctx.state.page_no)
        if profile.get("profiled") and profile.get("n_paths", 1) == 0:
            return OpResult(
                cost=Cost(duration_ms=timer.ms()),
                signals={"tables_found": 0},
                metadata={"skipped": "page has no vector paths"},
            )

    if strategy == "lines":
        settings: dict[str, Any] = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
    elif strategy == "text":
        settings = {"vertical_strategy": "text", "horizontal_strategy": "text"}
    else:
        settings = {}

    object_limit = int(params.get("max_vector_objects", VECTOR_OBJECT_LIMIT))
    try:
        page = ctx.session.plumber_page(ctx.state.page_no) if hasattr(ctx.session, "plumber_page") else None
        if page is None:
            return OpResult(cost=Cost(duration_ms=timer.ms()))
        if object_limit > 0:
            vectors = len(page.lines) + len(page.rects) + len(getattr(page, "curves", []))
            if vectors > object_limit:
                return OpResult(
                    cost=Cost(duration_ms=timer.ms()),
                    signals={"tables_found": 0},
                    diagnostics=[
                        f"table detection skipped: {vectors} vector objects exceeds {object_limit} "
                        "(a figure, not a table grid)"
                    ],
                    metadata={"skipped": f"{vectors} vector objects"},
                )
        found = page.find_tables(settings) if settings else page.find_tables()
    except Exception as exc:  # noqa: BLE001 - a failed extraction is reported, not raised
        return OpResult(
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[f"table extraction ({strategy}) failed: {type(exc).__name__}: {exc}"],
            signals={"tables_found": 0},
        )

    blocks: list[Block] = []
    rejected: list[str] = []
    for table in found:
        try:
            # pdfplumber yields None cells AND, for a malformed table, None rows.
            # Both are normal, and neither may crash the page.
            extracted = table.extract() or []
            rows = [
                ["" if cell is None else str(cell).strip() for cell in (row or [])]
                for row in extracted
                if row is not None
            ]
        except Exception as exc:  # noqa: BLE001
            rejected.append(f"extract failed: {exc}")
            continue
        rows = [row for row in rows if any(cell for cell in row)]
        if len(rows) < min_rows:
            rejected.append(f"only {len(rows)} non-empty row(s)")
            continue
        n_cols = max((len(row) for row in rows), default=0)
        if n_cols < min_cols:
            rejected.append(f"only {n_cols} column(s)")
            continue
        cells = [cell for row in rows for cell in row]
        empty = sum(1 for cell in cells if not cell)
        if cells and empty / len(cells) > max_empty:
            rejected.append(f"{empty}/{len(cells)} cells empty")
            continue
        x0, top, x1, bottom = [float(value) for value in table.bbox]
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:t{len(blocks)}",
                page_id=ctx.state.page_id,
                order=len(blocks),
                type="table",
                text=_table_to_text(rows),
                table=TablePayload(rows=rows, header_rows=int(params.get("header_rows", 1)), n_cols=n_cols, strategy=strategy),
                bbox=BBox(x0=x0, y0=top, x1=x1, y1=bottom),
                source_op="pdf_tables",
                confidence=0.9 if strategy == "lines" else 0.7,
            )
        )

    return OpResult(
        blocks=blocks,
        signals={"tables_found": len(blocks), "tables_rejected": len(rejected)},
        cost=Cost(duration_ms=timer.ms()),
        diagnostics=rejected[:5],
    )


def _table_to_text(rows: list[list[str]]) -> str:
    return "\n".join(" | ".join(cell for cell in row) for row in rows)


def op_office_tables(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Emit tables a PPTX or DOCX already stores as a table structure.

    Nothing is inferred here: the file says "this is a table" and gives the rows,
    which is strictly better information than any extractor could recover from
    pixels.
    """
    timer = Timer()
    session = ctx.session
    if session is None:
        return OpResult(cost=Cost(duration_ms=timer.ms()))

    blocks: list[Block] = []
    if hasattr(session, "section_text") and hasattr(session, "sections"):
        section = session.sections()[ctx.state.page_no - 1]
        for rows in section["tables"]:
            cleaned = [[str(cell or "").strip() for cell in row] for row in rows]
            cleaned = [row for row in cleaned if any(row)]
            if len(cleaned) < MIN_TABLE_ROWS:
                # A table too small or too empty to be a table is still CONTENT.
                #
                # Dropping it lost data silently: on a harvested Word fixture the
                # text layer carried `T2` while the blocks carried nothing, because
                # a 2x2 table has one non-empty row and failed the table test.
                # PDFs hid this — their text layer re-emits table characters as
                # paragraphs — but a DOCX emits paragraphs and tables from separate
                # operators, so a rejected table had no fallback at all.
                #
                # Structure may be downgraded. Content may not be dropped.
                text = " | ".join(cell for row in cleaned for cell in row if cell).strip()
                if text:
                    blocks.append(
                        Block(
                            block_id=f"{ctx.state.page_id}:tp{len(blocks)}",
                            page_id=ctx.state.page_id,
                            order=len(blocks),
                            type="paragraph",
                            text=text,
                            source_op="office_tables",
                            confidence=0.9,
                            checks={"downgraded_from": "table", "reason": "too few non-empty rows"},
                        )
                    )
                continue
            blocks.append(
                Block(
                    block_id=f"{ctx.state.page_id}:t{len(blocks)}",
                    page_id=ctx.state.page_id,
                    order=len(blocks),
                    type="table",
                    text=_table_to_text(cleaned),
                    table=TablePayload(rows=cleaned, header_rows=1, n_cols=max(len(r) for r in cleaned)),
                    source_op="office_tables",
                    confidence=1.0,
                    checks={"native": True},
                )
            )
    return OpResult(blocks=blocks, signals={"tables_found": len(blocks)}, cost=Cost(duration_ms=timer.ms()))


def op_reading_order(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Reorder this page's blocks into reading order.

    Two-column papers are the whole problem: a naive top-to-bottom sort
    interleaves the columns and produces text that reads as nonsense while
    looking fine in a diff. The column split is detected from the horizontal
    distribution of block centres, not assumed.
    """
    timer = Timer()
    blocks = [block for block in ctx.state.record.blocks if block.bbox is not None]
    unpositioned = [block for block in ctx.state.record.blocks if block.bbox is None]
    if not blocks:
        return OpResult(cost=Cost(duration_ms=timer.ms()), metadata={"ordered": False, "reason": "no bboxes"})

    width = ctx.state.record.signals.page_width or 612.0
    columns = _detect_columns(blocks, width)
    if len(columns) > 1:
        ordered = _order_by_columns(blocks, columns)
        strategy = f"columns:{len(columns)}"
    else:
        ordered = sorted(blocks, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))  # type: ignore[union-attr]
        strategy = "single_column"

    merged = ordered + unpositioned
    for index, block in enumerate(merged):
        block.order = index
    ctx.state.record.blocks = merged
    return OpResult(cost=Cost(duration_ms=timer.ms()), metadata={"ordering": strategy, "blocks": len(merged)})


def _detect_columns(blocks: list[Block], page_width: float) -> list[tuple[float, float]]:
    """Find vertical bands that hold text, treating a wide block as a separator."""
    if page_width <= 0:
        return []
    # Any block spanning most of the width means the page is not columnar at the
    # point it appears (a title, a full-width figure), so column detection is not
    # meaningful for this page.
    for block in blocks:
        if block.bbox and block.bbox.width > page_width * 0.75:
            return []
    centres = sorted(block.bbox.x0 for block in blocks if block.bbox)
    if len(centres) < 4:
        return []
    centres_page = [(block.bbox.x0 + block.bbox.x1) / 2 for block in blocks if block.bbox]
    centres_page.sort()
    mid = page_width / 2
    left = [value for value in centres_page if value < mid * 0.95]
    right = [value for value in centres_page if value > mid * 1.05]
    if len(left) < 2 or len(right) < 2:
        return []
    left_max = max(block.bbox.x1 for block in blocks if block.bbox and (block.bbox.x0 + block.bbox.x1) / 2 < mid * 0.95)
    right_min = min(block.bbox.x0 for block in blocks if block.bbox and (block.bbox.x0 + block.bbox.x1) / 2 > mid * 1.05)
    if right_min <= left_max:
        return []
    return [(0.0, left_max), (right_min, page_width)]


def _order_by_columns(blocks: list[Block], columns: list[tuple[float, float]]) -> list[Block]:
    def column_of(block: Block) -> int:
        assert block.bbox is not None
        centre = (block.bbox.x0 + block.bbox.x1) / 2
        for index, (start, end) in enumerate(columns):
            if start <= centre <= end:
                return index
        return 0

    return sorted(
        blocks,
        key=lambda b: (column_of(b), round(b.bbox.y0, 1)) if b.bbox else (0, 0.0),  # type: ignore[union-attr]
    )


def op_caption_blocks(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Mark caption lines, which is how figures and tables keep their labels.

    A caption is identified by its prefix (``Table 3:``, ``图 2``), never by
    proximity to a picture: proximity guesses wrong exactly where it matters, on
    pages with several figures.
    """
    timer = Timer()
    blocks: list[Block] = []
    for block in ctx.state.record.blocks:
        if block.type not in ("paragraph", "heading") or not block.text:
            continue
        match = CAPTION_PATTERN.match(block.text.strip())
        if not match:
            continue
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:c{len(blocks)}",
                page_id=ctx.state.page_id,
                order=block.order,
                type="caption",
                text=block.text.strip(),
                bbox=block.bbox,
                source_op="caption_blocks",
                confidence=0.8,
            )
        )
        # The original is superseded: the same characters must not appear twice.
        block.type = "paragraph"
        block.checks["superseded_by"] = "caption"
    if not blocks:
        return OpResult(cost=Cost(duration_ms=timer.ms()))
    ctx.state.record.blocks = [b for b in ctx.state.record.blocks if b.checks.get("superseded_by") != "caption"]
    return OpResult(blocks=blocks, signals={"captions": len(blocks)}, cost=Cost(duration_ms=timer.ms()))
