"""Quality gate and memory chunking.

The gate is the cost lever of the whole system. Everything before it is free;
everything after it costs money. So it must fail *loudly and narrowly*: loud
enough that a genuinely unreadable page always escalates, narrow enough that a
clean page never does.

Its thresholds are calibrated against two real failures found in this corpus —
a page whose text layer was largely ``(cid:NNN)`` placeholders, and a page the
model read confidently wrong from a 52 dpi raster — not against a synthetic
benchmark.
"""

from __future__ import annotations

import re
from typing import Any

from docpipe.api.types import Block, Cost, OpContext, OpResult, Timer

CID_PATTERN = re.compile(r"\(cid:\d+\)")

#: Above this fraction of replacement characters the text layer is untrustworthy.
REPLACEMENT_LIMIT = 0.02

#: A page with this much image coverage and this little text is a scan.
SCAN_IMAGE_FRACTION = 0.5
SCAN_TEXT_CEILING = 200

#: Rows that are entirely empty beyond this fraction mean the table failed.
TABLE_EMPTY_LIMIT = 0.3


def op_gate_default(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Score this page and decide whether it needs escalation.

    Reports rather than decides: the executor reads ``quality.needs_escalation``
    and applies the plan's ladder, so the policy stays in one place instead of
    being duplicated in every operator.
    """
    timer = Timer()
    record = ctx.state.record
    text = record.text_layer or ""
    stripped = text.strip()
    chars = len(stripped)
    signals = record.signals

    text_floor = int(params.get("text_floor_chars", getattr(ctx.config, "gate_text_floor_chars", 80)))
    image_fraction = float(getattr(ctx.config, "gate_image_area_frac", SCAN_IMAGE_FRACTION))

    # The gate asks "is there usable content on this page?", not "is there a text
    # layer?". A page the vision model has just transcribed has content — marking
    # it for review would contradict the extraction that succeeded, and it did:
    # all four image-only slides came back `needs_review` while carrying 69 good
    # blocks between them.
    block_chars = sum(len(block.text or "") for block in record.blocks)
    vision_sourced = any(block.source_op.startswith("vl_") for block in record.blocks)
    content_chars = max(chars, block_chars)

    reasons: list[str] = []
    metrics: dict[str, Any] = {
        "text_layer_chars": chars,
        "block_text_chars": block_chars,
        "vision_sourced": vision_sourced,
        "blocks": len(record.blocks),
    }

    # 1. Encoding health. A page can have plenty of characters and still be
    #    worthless — but only when those characters are what we are relying on.
    #    If the content came from the model, a garbled layer is irrelevant.
    if stripped and not vision_sourced:
        cid_hits = len(CID_PATTERN.findall(stripped))
        replacement_ratio = stripped.count("\ufffd") / len(stripped)
        metrics["cid_hits"] = cid_hits
        metrics["replacement_ratio"] = round(replacement_ratio, 4)
        if cid_hits > 20 or replacement_ratio > REPLACEMENT_LIMIT:
            reasons.append(
                f"text layer is garbled ({cid_hits} CID placeholders, "
                f"{replacement_ratio:.1%} replacement characters)"
            )

    # 2. Scanned page: lots of ink, no content.
    image_area = signals.image_area_frac
    metrics["image_area_frac"] = image_area
    if image_area is not None and image_area >= image_fraction and content_chars < SCAN_TEXT_CEILING:
        reasons.append(f"image area {image_area:.0%} with only {content_chars} characters of content")

    # 3. Empty page that should not be empty.
    if content_chars == 0 and not record.blocks:
        reasons.append("no text layer and no blocks")

    # 4. Thin content on a page that clearly has something.
    elif 0 < content_chars < text_floor and record.kind == "pdf_page":
        if image_area is None or image_area > 0.15:
            reasons.append(
                f"only {content_chars} characters of content (< {text_floor}) "
                f"with {image_area if image_area is not None else 'unknown'} image coverage"
            )

    # 5. Text-layer trust. The gate's other checks ask whether the text layer is
    #    present and not obviously broken. This one asks whether it can be BELIEVED,
    #    which is a different question and the one that catches an OCR layer.
    #
    #    The motivating document renders `T(R)` as `~'(R)`, `R^[1]` as `R tl]` and
    #    `∪{R[y] × yS | y ∈ Y}` as `U{Ry x ySI y E Y}` — while presenting 2447
    #    characters a page, 195 words and nine replacement characters. Every other
    #    check passes it, and its extracted text is quietly wrong about every
    #    formula in the paper.
    trust = getattr(signals, "text_layer_trust", 1.0)
    metrics["text_layer_trust"] = trust
    trust_floor = float(params.get("trust_floor", getattr(ctx.config, "gate_trust_floor", 0.4)))
    if stripped and trust < trust_floor and not vision_sourced:
        # The layer is still an OCR layer, but nothing is being read from it any
        # more: the content came from the page image. Distrusting it again would
        # mark every successfully transcribed page for review.
        reasons.append(
            f"text layer cannot be trusted (trust {trust:.2f} < {trust_floor}): unembedded fonts "
            "without unicode maps mean characters are inferred, not read"
        )

    # 6. Column structure. A flat text layer preserves lines but not columns, so on
    #    a two-column page the columns interleave and the text reads as nonsense
    #    while looking fine in a diff. Layout-aware extraction needs geometry: a
    #    raster and a model, or a layout engine.
    columns = int(getattr(signals, "columns", 1) or 1)
    metrics["columns"] = columns
    if columns >= 2 and stripped and not vision_sourced and not params.get("columns_handled", False):
        reasons.append(
            f"page has {columns} text columns; a flat text layer interleaves them and "
            "needs layout-aware extraction"
        )

    # 5. Tables that were detected but not extracted.
    candidates = signals.n_table_candidates or 0
    extracted = sum(1 for block in record.blocks if block.type == "table")
    if candidates and not extracted:
        reasons.append(f"{candidates} table candidate(s) detected but none extracted")
    for block in record.blocks:
        if block.type != "table" or not block.table:
            continue
        cells = [cell for row in block.table.rows for cell in row]
        if not cells:
            continue
        empty_ratio = sum(1 for cell in cells if not (cell or "").strip()) / len(cells)
        if empty_ratio > TABLE_EMPTY_LIMIT:
            reasons.append(f"table with {empty_ratio:.0%} empty cells")
            break

    # 6. Reading-order sanity: a page whose text disappeared into the assembly is
    #    as broken as one that never had text.
    if chars > 400 and not record.blocks:
        reasons.append("text layer present but no blocks were produced")

    metrics["reasons"] = reasons
    metrics["needs_escalation"] = bool(reasons)
    metrics["gate"] = "default_v1"

    return OpResult(
        signals={"gate_failed": bool(reasons)},
        cost=Cost(duration_ms=timer.ms()),
        metadata={"quality": metrics},
        diagnostics=[f"gate: {reason}" for reason in reasons],
    )


def op_chunk_for_memory(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Group the page's blocks into retrieval-sized chunks.

    Chunking happens here, at the end of a page's pipeline, because this is the
    last point where the heading path, the page number and the source operator
    are all still in hand. Reconstituting provenance later is guesswork.
    """
    timer = Timer()
    target = int(params.get("target_chars", 1200))
    max_chars = int(params.get("max_chars", 2000))
    min_chars = int(params.get("min_chars", 200))

    blocks = sorted(ctx.state.record.blocks, key=lambda b: b.order)
    chunks: list[dict[str, Any]] = []
    heading_path: list[str] = []
    buffer: list[Block] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        if not buffer:
            return
        text = "\n\n".join(block.text for block in buffer if block.text).strip()
        block_ids = [block.block_id for block in buffer]
        buffer = []
        size = 0
        if not text:
            return
        if len(text) < min_chars and chunks:
            # Append a runt to the previous chunk instead of emitting a stub.
            chunks[-1]["text"] += "\n\n" + text
            chunks[-1]["block_ids"].extend(block_ids)
            return
        chunks.append(
            {
                "chunk_id": f"{ctx.state.page_id}:c{len(chunks)}",
                "page_id": ctx.state.page_id,
                "heading_path": list(heading_path),
                "text": text,
                "block_ids": block_ids,
            }
        )

    for block in blocks:
        if block.type == "heading" and block.text:
            level = block.level or 1
            heading_path = heading_path[: max(0, level - 1)]
            heading_path.append(block.text)
        if block.type == "header_footer":
            continue
        buffer.append(block)
        size += len(block.text or "")
        if size >= target:
            flush()
    flush()

    for index, chunk in enumerate(chunks):
        chunk["chunk_id"] = f"{ctx.state.page_id}:c{index}"
    return OpResult(
        signals={"chunks": len(chunks)},
        cost=Cost(duration_ms=timer.ms()),
        metadata={"chunks": chunks},
    )
