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


SHARDS = "shards"


def shard_dir(out_dir: Path) -> Path:
    path = out_dir / SHARDS
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_shard(out_dir: Path, doc_id: str, page_rows: Sequence[dict[str, Any]], block_rows: Sequence[dict[str, Any]]) -> None:
    """Write one document's rows as files that are complete the moment they exist.

    The run's `pages.parquet` and `blocks.parquet` are streams: their footer is
    written at close, so a process killed by a segfault — or an OOM, or a full
    disk — leaves files that contain every byte of finished work and no way to
    read any of it. Measured: a 357-page issue segfaulted PDFium on the 16th
    document of a corpus run and took 15 completed documents with it.

    These shards are merged by `docpipe recover`, and are cheap: one small file
    pair per document, written once.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    target = shard_dir(out_dir)
    pq.write_table(pa.Table.from_pylist(page_rows, schema=page_schema()), target / f"{doc_id}.pages.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(block_rows, schema=block_schema()), target / f"{doc_id}.blocks.parquet", compression="zstd")
    write_json(target / f"{doc_id}.done", {"doc_id": doc_id, "pages": len(page_rows), "blocks": len(block_rows)})


def recover(out_dir: Path) -> dict[str, int]:
    """Rebuild the run's artifacts from per-document shards.

    For a run that died mid-document, this is the difference between "the output
    is a corrupt parquet file" and "fifteen documents are readable and the
    sixteenth is missing". Returns counts of what was recovered.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    target = out_dir / SHARDS
    pages = sorted(target.glob("*.pages.parquet"))
    blocks = sorted(target.glob("*.blocks.parquet"))
    if not pages:
        raise FileNotFoundError(f"no shards in {target}")

    page_tables = [pq.read_table(path) for path in pages]
    block_tables = [pq.read_table(path) for path in blocks if path.stat().st_size]
    merged_pages = pa.concat_tables(page_tables) if page_tables else pa.Table.from_pylist([], schema=page_schema())
    merged_blocks = pa.concat_tables(block_tables) if block_tables else pa.Table.from_pylist([], schema=block_schema())
    pq.write_table(merged_pages, out_dir / "pages.parquet", compression="zstd")
    pq.write_table(merged_blocks, out_dir / "blocks.parquet", compression="zstd")

    documents = []
    progress = out_dir / "documents.jsonl"
    if progress.is_file():
        for line in progress.read_text(encoding="utf-8").splitlines():
            if line.strip():
                documents.append(json.loads(line))

    # The manifest is written at the END of a run, so a crashed run has none — and
    # the evaluator resolves each document's source through it, which left every
    # recovered document reporting "source file is gone" and nothing measured.
    # The progress rows carry the same fields, so the manifest can be rebuilt.
    if documents:
        manifest = [
            {
                "doc_id": row.get("doc_id"),
                "source_path": row.get("source_path"),
                "format": row.get("format"),
                "bytes": 0,
                "sha256": row.get("doc_id"),
                "units": row.get("units", 0),
                "kind": "",
                "pipeline_id": row.get("pipeline_id"),
            }
            for row in documents
            if row.get("doc_id") and row.get("source_path")
        ]
        write_json(out_dir / MANIFEST, manifest)
    return {"documents": len(documents), "pages": merged_pages.num_rows, "blocks": merged_blocks.num_rows}


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
        "vl_cache_hits": sum(doc.vl_cache_hits for doc in docs),
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
