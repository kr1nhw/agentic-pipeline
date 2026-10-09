"""Artifact layout, streaming writers and exporters.

Everything a run produces lands in one flat, self-describing tree, and every
writer streams: a 1428-page document must never be assembled in memory to be
written out. Row groups flush as pages complete, so a killed run leaves a
readable partial artifact and `--resume` continues into it.

The canonical IR is `pages.parquet` + `blocks.parquet`. Markdown, JSONL and the
memory chunks are *derived* views — anything not reproducible from the IR is a
bug, not a feature.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from docpipe.schema import Block, DocumentRecord, DocReport, PageRecord, RunReport

#: Flush a parquet row group every this many pages.
ROW_GROUP_PAGES = 256

MANIFEST = "manifest.json"
REPORT = "report.json"


def doc_dir(out_dir: Path, doc_id: str) -> Path:
    path = out_dir / "docs" / doc_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Any) -> None:
    """Atomic write: a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    tmp.replace(path)


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            count += 1
    return count


class ParquetStream:
    """Append-only parquet writer with a bounded row-group buffer."""

    def __init__(self, path: Path, schema: Any, *, flush_every: int = ROW_GROUP_PAGES) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema = schema
        self._pa = pa
        self._writer = pq.ParquetWriter(path, schema, compression="zstd")
        self._buffer: list[dict[str, Any]] = []
        self._flush_every = flush_every
        self.rows = 0

    def write(self, row: dict[str, Any]) -> None:
        self._buffer.append(row)
        if len(self._buffer) >= self._flush_every:
            self.flush()

    def write_many(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.write(row)

    def flush(self) -> None:
        if not self._buffer:
            return
        table = self._pa.Table.from_pylist(self._buffer, schema=self._schema)
        self._writer.write_table(table)
        self.rows += len(self._buffer)
        self._buffer = []

    def close(self) -> None:
        self.flush()
        self._writer.close()

    def __enter__(self) -> "ParquetStream":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def page_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("doc_id", pa.string()),
            ("page_no", pa.int32()),
            ("page_id", pa.string()),
            ("kind", pa.string()),
            ("route", pa.string()),
            ("pipeline_id", pa.string()),
            ("text_layer_chars", pa.int32()),
            ("text_layer", pa.string()),
            ("signals", pa.string()),
            ("quality", pa.string()),
            ("raster_uri", pa.string()),
            ("n_blocks", pa.int32()),
            ("text", pa.string()),
            ("vl_calls", pa.int32()),
            ("prompt_tokens", pa.int32()),
            ("completion_tokens", pa.int32()),
            ("usd_est", pa.float64()),
            ("duration_ms", pa.int32()),
            ("errors", pa.string()),
            ("provenance", pa.string()),
        ]
    )


def block_schema() -> Any:
    import pyarrow as pa

    return pa.schema(
        [
            ("block_id", pa.string()),
            ("doc_id", pa.string()),
            ("page_id", pa.string()),
            ("page_no", pa.int32()),
            ("order", pa.int32()),
            ("type", pa.string()),
            ("text", pa.string()),
            ("level", pa.int32()),
            ("source_op", pa.string()),
            ("confidence", pa.float64()),
            ("bbox_x0", pa.float64()),
            ("bbox_y0", pa.float64()),
            ("bbox_x1", pa.float64()),
            ("bbox_y1", pa.float64()),
            ("table_rows", pa.int32()),
            ("table_cols", pa.int32()),
            ("table_json", pa.string()),
            ("image_uri", pa.string()),
            ("checks", pa.string()),
        ]
    )


def page_row(record: PageRecord) -> dict[str, Any]:
    signals = record.signals.model_dump(exclude_none=False)
    return {
        "doc_id": record.doc_id,
        "page_no": record.page_no,
        "page_id": record.page_id,
        "kind": record.kind,
        "route": record.route,
        "pipeline_id": record.pipeline_id,
        "text_layer_chars": record.text_layer_chars,
        "text_layer": record.text_layer,
        "signals": json.dumps(signals, ensure_ascii=False),
        "quality": json.dumps(record.quality, ensure_ascii=False),
        "raster_uri": record.raster_uri,
        "n_blocks": len(record.blocks),
        "text": record.text,
        "vl_calls": record.vl_calls,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "usd_est": record.usd_est,
        "duration_ms": record.duration_ms,
        "errors": json.dumps(record.errors[:20], ensure_ascii=False),
        "provenance": json.dumps(record.provenance, ensure_ascii=False, default=str),
    }


def block_rows(record: PageRecord) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block in record.blocks:
        bbox = block.bbox
        table = block.table
        rows.append(
            {
                "block_id": block.block_id,
                "doc_id": record.doc_id,
                "page_id": record.page_id,
                "page_no": record.page_no,
                "order": block.order,
                "type": block.type,
                "text": block.text,
                "level": block.level,
                "source_op": block.source_op,
                "confidence": float(block.confidence),
                "bbox_x0": bbox.x0 if bbox else None,
                "bbox_y0": bbox.y0 if bbox else None,
                "bbox_x1": bbox.x1 if bbox else None,
                "bbox_y1": bbox.y1 if bbox else None,
                "table_rows": len(table.rows) if table else None,
                "table_cols": table.n_cols if table else None,
                "table_json": json.dumps(table.model_dump(), ensure_ascii=False) if table else None,
                "image_uri": block.image.uri if block.image else None,
                "checks": json.dumps(block.checks, ensure_ascii=False, default=str),
            }
        )
    return rows


def render_markdown(record: PageRecord, *, heading_offset: int = 0) -> str:
    """One page's blocks as Markdown, in reading order."""
    lines: list[str] = [f"<!-- page: {record.page_no} kind: {record.kind} route: {record.route} -->"]
    for block in sorted(record.blocks, key=lambda b: b.order):
        if block.type == "heading":
            level = min(6, max(1, (block.level or 1) + heading_offset))
            lines.append(f"{'#' * level} {block.text}".rstrip())
        elif block.type == "table" and block.table:
            lines.append(_markdown_table(block.table.rows))
        elif block.type == "code":
            language = block.checks.get("language", "")
            lines.append(f"```{language}\n{block.text}\n```")
        elif block.type == "figure":
            caption = (block.image.caption if block.image else None) or ""
            uri = block.image.uri if block.image else None
            lines.append(f"![{caption}]({uri or ''})")
        elif block.type == "formula":
            lines.append(f"$$\n{block.text}\n$$")
        elif block.type in ("header_footer",):
            continue
        elif block.text:
            lines.append(block.text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _markdown_table(rows: Sequence[Sequence[str | None]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalised = [[(cell or "").replace("|", "\\|") for cell in row] + [""] * (width - len(row)) for row in rows]
    header = normalised[0]
    body = normalised[1:]
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
    out.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(out)


def write_document_markdown(out_dir: Path, record: DocumentRecord, pages: Iterable[PageRecord]) -> Path:
    target = doc_dir(out_dir, record.doc_id) / "content.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        handle.write(f"# {Path(record.source_path).name}\n\n")
        handle.write(f"<!-- doc_id: {record.doc_id} format: {record.format} units: {record.units} -->\n\n")
        for page in pages:
            handle.write(render_markdown(page))
            handle.write("\n")
    return target


def write_document_jsonl(out_dir: Path, record: DocumentRecord, pages: Iterable[PageRecord]) -> Path:
    target = doc_dir(out_dir, record.doc_id) / "content.jsonl"
    rows = []
    for page in pages:
        for block in sorted(page.blocks, key=lambda b: b.order):
            rows.append(
                {
                    "doc_id": record.doc_id,
                    "page_id": page.page_id,
                    "page_no": page.page_no,
                    "block_id": block.block_id,
                    "order": block.order,
                    "type": block.type,
                    "text": block.text,
                    "level": block.level,
                    "source_op": block.source_op,
                    "confidence": block.confidence,
                    "table": block.table.model_dump() if block.table else None,
                    "image": block.image.model_dump() if block.image else None,
                }
            )
    append_jsonl(target, rows)
    return target


def summarize_report(
    *,
    version: str,
    api_version: str,
    docs: Sequence[DocReport],
    started: Any,
    finished: Any,
    budget: dict[str, Any],
    plugins: list[dict[str, Any]],
    model: str | None,
    offline: bool,
    engine: str,
    shard: str | None,
    stopped_reason: str | None,
) -> RunReport:
    totals: dict[str, Any] = {
        "documents": len(docs),
        "units": sum(doc.units for doc in docs),
        "vl_calls": sum(doc.vl_calls for doc in docs),
        "prompt_tokens": sum(doc.prompt_tokens for doc in docs),
        "completion_tokens": sum(doc.completion_tokens for doc in docs),
        "usd_est": round(sum(doc.usd_est for doc in docs), 6),
        "needs_review": sum(len(doc.needs_review) for doc in docs),
        "gate_failures": sum(doc.gate_failures for doc in docs),
        "escalations": sum(doc.escalations for doc in docs),
        "route_counts": _merge_route_counts(docs),
        **budget,
    }
    return RunReport(
        docpipe_version=version,
        api_version=api_version,
        started_at=started,
        finished_at=finished,
        model=model,
        offline=offline,
        engine=engine,
        shard=shard,
        docs=list(docs),
        totals=totals,
        plugins=plugins,
        stopped_reason=stopped_reason,
    )


def _merge_route_counts(docs: Sequence[DocReport]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for doc in docs:
        for route, count in doc.route_counts.items():
            merged[route] = merged.get(route, 0) + count
    return merged
