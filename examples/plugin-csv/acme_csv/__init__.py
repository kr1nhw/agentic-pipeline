"""A CSV/TSV format plugin: the smallest complete extension.

Two things are worth copying from this file:

* The matcher looks at *bytes* as well as the extension, and returns a confidence
  rather than a boolean, so a real parser can outrank a sniff.
* The operator declares what it produces and what it costs. The registry refuses
  a ``vl`` operator from a plugin without ``model_calls``, and a planner can only
  order operators whose ``requires`` are satisfied.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterator

from docpipe.api import OpResult, UnitSeed
from docpipe.api.types import Block, Cost, PageSignals, TablePayload, Timer

#: Rows per unit. A 2 GB export must stream; one unit per file would not.
ROWS_PER_UNIT = 5000

#: Sample size for the content sniff.
SNIFF_LINES = 5


def match(head: bytes, path: str) -> float:
    lowered = str(path).lower()
    if lowered.endswith((".csv", ".tsv")):
        return 0.95
    # Content sniff: several lines with a consistent, small delimiter count.
    try:
        text = head.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return 0.0
    lines = [line for line in text.splitlines() if line.strip()][:SNIFF_LINES]
    if len(lines) < 3:
        return 0.0
    for delimiter in (",", "\t", ";", "|"):
        counts = {line.count(delimiter) for line in lines}
        if len(counts) == 1 and counts.pop() >= 1:
            return 0.6
    return 0.0


def _delimiter(path: Path) -> str:
    return "\t" if str(path).lower().endswith(".tsv") else ","


def enumerate_units(ctx: Any) -> Iterator[UnitSeed]:
    """Yield fixed-size row blocks.

    Seeking is not required to find unit N — this is a single forward pass — so the
    format declares ``seekable=False`` and the runtime knows not to shard it.
    """
    from docpipe.ingest.textlike import read_text

    text = read_text(ctx.path)
    reader = csv.reader(io.StringIO(text), delimiter=_delimiter(ctx.path))
    header: list[str] | None = None
    batch: list[list[str]] = []
    unit = 0

    def seed(rows: list[list[str]], first: bool) -> UnitSeed:
        preview = "\n".join(" | ".join(row) for row in rows[:3])
        return UnitSeed(
            page_no=unit,
            kind="text_chunk",
            signals=PageSignals(
                text_layer_chars=sum(len(cell) for row in rows for cell in row),
                has_table_candidate=True,
                n_table_candidates=1,
                layout_hint="tabular",
                text_sample=preview[:220] or None,
            ),
            payload={"rows": rows, "header": header if first else None, "columns": len(header or [])},
        )

    for row in reader:
        if header is None:
            header = row
            continue
        batch.append(row)
        if len(batch) >= ROWS_PER_UNIT:
            unit += 1
            yield seed(batch, first=unit == 1)
            batch = []
    if batch or unit == 0:
        unit += 1
        yield seed(batch, first=unit == 1)


def csv_rows(context: Any, params: dict[str, Any]) -> OpResult:
    """Emit this unit's rows as a table block."""
    timer = Timer()
    payload = context.state.record.signals.model_extra or {}
    rows = (context.state.scratch.get("rows") or [])
    header = context.state.scratch.get("header")
    if not rows:
        return OpResult(cost=Cost(duration_ms=timer.ms()), diagnostics=["no rows in this unit"])

    max_rows = int(params.get("max_rows", 0))
    if max_rows and len(rows) > max_rows:
        rows = rows[:max_rows]

    table_rows = ([header] if header else []) + rows
    block = Block(
        block_id=f"{context.state.page_id}:csv",
        page_id=context.state.page_id,
        order=0,
        type="table",
        text="\n".join(" | ".join(row) for row in table_rows),
        table=TablePayload(rows=table_rows, header_rows=1 if header else 0, n_cols=len(table_rows[0]), strategy="native"),
        source_op="csv_rows",
        confidence=1.0,
        checks={"delimiter": params.get("delimiter") or "auto"},
    )
    del payload  # signals are the executor's to merge, not ours to rewrite
    return OpResult(blocks=[block], signals={"tables_found": 1}, cost=Cost(duration_ms=timer.ms()))


def register(api: Any) -> None:
    api.register_format(
        "csv",
        match=match,
        enumerate_units=enumerate_units,
        kind="text_chunk",
        extensions=(".csv", ".tsv"),
        default_ops=("csv_rows",),
        # A single forward pass: unit N cannot be reached without reading 1..N-1.
        seekable=False,
        streaming=True,
    )
    api.register_op(
        "csv_rows",
        csv_rows,
        summary=(
            "Emit a CSV/TSV unit's rows as one table block. Native and exact: the cells come "
            "from the parser, not from a layout reconstruction. Use it instead of any vision "
            "table extraction on delimited text."
        ),
        params_schema={
            "delimiter": {"type": "string", "maxLength": 2},
            "max_rows": {"type": "integer", "default": 0, "min": 0},
        },
        cost_class="local",
        unit_kinds=("text_chunk",),
        produces=("blocks", "table"),
    )
