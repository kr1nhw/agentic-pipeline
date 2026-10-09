"""Raster and vision operators — the expensive tier, and the only gated one.

Two rules govern everything here:

1. **A raster must be legible.** The model invented a page title at 52 dpi that
   the text layer flatly contradicted. Below ``min_extraction_dpi`` this module
   refuses to call the model at all, because a confident wrong answer is worse
   than no answer.
2. **Vision never silently wins.** When a text layer exists, ``vl_cross_check``
   compares the two and records the disagreement. Replacing ground truth with a
   plausible-looking reading is the single most damaging failure this system
   could have.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from docpipe.api.types import Block, BBox, Cost, OpContext, OpResult, TablePayload, Timer

FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

EXTRACT_SYSTEM = (
    "You transcribe document pages into structured JSON. You report only what the page shows; "
    "when a region is illegible you say so instead of guessing. You never summarise, translate or "
    "improve the text, and you never invent content that is not in the image."
)

VALID_BLOCK_TYPES = {
    "heading", "paragraph", "list", "table", "figure", "formula", "caption", "code", "footnote", "meta"
}


def _parse_json(text: str) -> dict[str, Any] | None:
    """Recover a JSON object from a model reply.

    Models wrap JSON in fences, prepend a sentence, or emit a bare object. All
    three are normal here; none of them is a reason to fail a page.
    """
    if not text:
        return None
    fenced = FENCE_PATTERN.search(text)
    candidate = fenced.group(1) if fenced else text
    for attempt in (candidate.strip(), None):
        if attempt is None:
            match = JSON_OBJECT.search(candidate)
            if not match:
                return None
            attempt = match.group(0)
        try:
            parsed = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _blocks_from_payload(ctx: OpContext, payload: dict[str, Any], source_op: str) -> list[Block]:
    """Convert a model payload into validated blocks, dropping anything invalid."""
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        # Tolerate the common shorthand: a bare text field plus tables.
        raw_blocks = []
        if isinstance(payload.get("text"), str):
            raw_blocks.append({"type": "paragraph", "text": payload["text"]})
    blocks: list[Block] = []
    for entry in raw_blocks:
        if not isinstance(entry, dict):
            continue
        block_type = str(entry.get("type", "paragraph")).lower()
        if block_type not in VALID_BLOCK_TYPES:
            block_type = "paragraph"
        text = entry.get("text")
        text = str(text).strip() if text is not None else ""
        if not text and block_type != "figure":
            continue
        level = entry.get("level")
        try:
            level = int(level) if level is not None else None
        except (TypeError, ValueError):
            level = None
        confidence = entry.get("confidence", 0.6)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.6
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:v{len(blocks)}",
                page_id=ctx.state.page_id,
                order=len(blocks),
                type=block_type,  # type: ignore[arg-type]
                text=text,
                level=level if block_type == "heading" else None,
                source_op=source_op,
                confidence=confidence,
                checks={"model": True, **( {"illegible": True} if entry.get("illegible") else {})},
            )
        )
    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# Rasterisation
# ─────────────────────────────────────────────────────────────────────────────


def op_page_raster(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Render this unit to a PNG and publish it as the ``raster`` artifact.

    Every ``vl_*`` operator requires this artifact, which is how the planner is
    prevented from asking a model to read a page that was never rendered.
    """
    timer = Timer()
    dpi = int(params.get("dpi", ctx.config.table_dpi if params.get("for_tables") else 150))
    floor = int(getattr(ctx.config, "min_extraction_dpi", 110))
    if dpi < floor:
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[
                f"refusing to rasterise at {dpi} dpi: below the {floor} dpi legibility floor "
                "(under-resolved pages make the model invent content)"
            ],
        )
    try:
        path = ctx.raster(dpi=dpi, force=bool(params.get("force", False)))
    except Exception as exc:  # noqa: BLE001
        return OpResult(ok=False, cost=Cost(duration_ms=timer.ms()), diagnostics=[f"raster failed: {exc}"])
    return OpResult(
        artifacts={"raster": str(path)},
        signals={"raster_dpi": dpi, "raster_bytes": path.stat().st_size if path.exists() else 0, "raster_uri": str(path)},
        cost=Cost(duration_ms=timer.ms()),
        metadata={"raster": str(path)},
    )


def op_img_probe(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Cheap statistics for a standalone image unit."""
    timer = Timer()
    session = ctx.session
    info: dict[str, Any] = {}
    if session is not None and hasattr(session, "probe"):
        try:
            info = session.probe()
        except Exception as exc:  # noqa: BLE001
            return OpResult(cost=Cost(duration_ms=timer.ms()), diagnostics=[f"probe failed: {exc}"])
    return OpResult(
        signals={
            "image_width": info.get("width"),
            "image_height": info.get("height"),
            "ink_ratio": info.get("ink_ratio"),
        },
        cost=Cost(duration_ms=timer.ms()),
        metadata=info,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Vision
# ─────────────────────────────────────────────────────────────────────────────


def _raster_for(ctx: OpContext, params: dict[str, Any]) -> Path | None:
    """The raster this page already has, or None if it has none."""
    existing = ctx.state.artifacts.get("raster")
    if existing and Path(existing).exists():
        return Path(existing)
    dpi = int(params.get("dpi", 150))
    try:
        return ctx.raster_path(dpi=dpi)
    except Exception:  # noqa: BLE001
        return None


def op_vl_extract(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Transcribe the page with the vision model.

    This is the escalation path, not the default. It is reached only for pages
    where the deterministic operators are known to have failed, and its output
    replaces theirs rather than being appended, because two contradictory
    readings of one page is worse than one reading that is marked as uncertain.
    """
    timer = Timer()
    if ctx.vl is None:
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=["vision is unavailable (offline, no key, or budget exhausted)"],
        )
    raster = _raster_for(ctx, params)
    if raster is None or not raster.exists():
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=["no raster for this unit; run a raster operator first"],
        )

    tasks = params.get("tasks") or ["ocr", "layout"]
    if isinstance(tasks, str):
        tasks = [tasks]
    dpi = int(params.get("dpi", 150))
    if dpi < int(getattr(ctx.config, "min_extraction_dpi", 110)):
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[f"refusing to transcribe a {dpi} dpi raster (legibility floor)"],
        )

    prior = (ctx.state.record.text_layer or "").strip()
    instruction = _build_instruction(tasks, prior)
    try:
        response = ctx.vl.complete(
            system=EXTRACT_SYSTEM,
            user=instruction,
            images=[raster],
            thinking=bool(params.get("thinking", getattr(ctx.config, "classify_thinking", False))),
            max_tokens=int(params.get("max_tokens", 4000)),
            json_object=True,
        )
    except Exception as exc:  # noqa: BLE001 - provider failures are page failures
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[f"vision call failed: {type(exc).__name__}: {exc}"],
        )

    payload = _parse_json(response.text)
    cost = Cost(
        vl_calls=1,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        reasoning_tokens=response.reasoning_tokens,
        usd=response.usd,
        duration_ms=timer.ms(),
    )
    if payload is None:
        return OpResult(
            ok=False,
            cost=cost,
            diagnostics=["vision returned no parseable JSON"],
            metadata={"raw": response.text[:400]},
        )

    blocks = _blocks_from_payload(ctx, payload, "vl_extract")
    tables = payload.get("tables")
    if isinstance(tables, list):
        for entry in tables:
            if not isinstance(entry, dict):
                continue
            rows = entry.get("rows")
            if not isinstance(rows, list) or not rows:
                continue
            cleaned = [[None if cell is None else str(cell).strip() for cell in row] for row in rows if isinstance(row, list)]
            if len(cleaned) < 2:
                continue
            blocks.append(
                Block(
                    block_id=f"{ctx.state.page_id}:vt{len(blocks)}",
                    page_id=ctx.state.page_id,
                    order=len(blocks),
                    type="table",
                    text="\n".join(" | ".join(cell or "" for cell in row) for row in cleaned),
                    table=TablePayload(rows=cleaned, header_rows=1, n_cols=max(len(r) for r in cleaned), strategy="vision"),
                    source_op="vl_extract",
                    confidence=0.55,
                    checks={"model": True},
                )
            )

    if not blocks:
        return OpResult(
            ok=False,
            cost=cost,
            diagnostics=["vision produced no usable blocks"],
        )

    return OpResult(
        blocks=blocks,
        replace_blocks=bool(params.get("replace", True)),
        cost=cost,
        signals={"vl_extract_dpi": dpi, "vl_tasks": ",".join(str(t) for t in tasks)},
        metadata={"model": response.model, "raw_chars": len(response.text)},
        diagnostics=[f"vision transcription used {response.completion_tokens} completion tokens"],
    )


def _build_instruction(tasks: list[Any], prior: str) -> str:
    task_list = ", ".join(str(task) for task in tasks)
    lines = [
        f"Transcribe this page. Tasks required: {task_list}.",
        "",
        "Return JSON only, with this shape:",
        '{"blocks":[{"type":"heading|paragraph|table|figure|formula|caption|code|list",'
        '"text":"...","level":1,"confidence":0.0-1.0,"illegible":false}],'
        '"tables":[{"rows":[["cell","cell"]]}],'
        '"notes":"anything you could not read"}',
        "",
        "Rules: preserve the original language and wording; keep headings as headings; "
        "put each table in both `blocks` and `tables`; mark unreadable regions with "
        '"illegible": true rather than guessing.',
    ]
    if prior:
        lines += [
            "",
            "The file's own text layer for this page follows. It may be incomplete or garbled "
            "(that is why you are being asked). Use it to spell names and numbers correctly, "
            "but correct it where the image disagrees, and never copy text that the image does "
            "not show:",
            "---",
            prior[:4000],
            "---",
        ]
    return "\n".join(lines)


def op_vl_page_classify(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Ask the model what kind of page this is, cheaply.

    Thinking is off by default: classification is a short, structured answer, and
    thinking mode costs an order of magnitude more output tokens for the same
    result. The answer is advisory — it informs routing, it never becomes content.
    """
    timer = Timer()
    if ctx.vl is None:
        return OpResult(ok=False, cost=Cost(duration_ms=timer.ms()), diagnostics=["vision unavailable"])
    raster = _raster_for(ctx, params)
    if raster is None or not raster.exists():
        return OpResult(ok=False, cost=Cost(duration_ms=timer.ms()), diagnostics=["no raster for classification"])

    question = str(
        params.get(
            "question",
            "Classify this document page. Return JSON only: "
            '{"page_role":"title|body|table|figure|mixed|references|form|slide","has_table":bool,'
            '"has_figure":bool,"formula":bool,"text_quality":"clean|noisy|scanned","read_title":"..."}',
        )
    )
    try:
        response = ctx.vl.complete(
            system=EXTRACT_SYSTEM,
            user=question,
            images=[raster],
            thinking=bool(params.get("thinking", getattr(ctx.config, "classify_thinking", False))),
            max_tokens=int(params.get("max_tokens", 512)),
            json_object=True,
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            ok=False,
            cost=Cost(duration_ms=timer.ms()),
            diagnostics=[f"classification failed: {type(exc).__name__}: {exc}"],
        )
    payload = _parse_json(response.text) or {}
    return OpResult(
        signals={"vl_page_role": payload.get("page_role"), "vl_has_table": payload.get("has_table")},
        cost=Cost(
            vl_calls=1,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            reasoning_tokens=response.reasoning_tokens,
            usd=response.usd,
            duration_ms=timer.ms(),
        ),
        metadata={"classification": payload},
    )


def op_vl_cross_check(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Check a vision transcription against the text layer and record disagreement.

    The text layer wins when it exists. This operator exists to make that
    explicit and visible: a page whose vision reading contradicts its own text
    layer is reported, not silently preferred.
    """
    timer = Timer()
    prior = (ctx.state.record.text_layer or "").strip()
    vision_blocks = [b for b in ctx.state.record.blocks if b.source_op.startswith("vl_")]
    if not prior or not vision_blocks:
        return OpResult(cost=Cost(duration_ms=timer.ms()), metadata={"cross_check": "not_applicable"})

    def tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[\w\u4e00-\u9fff]{4,}", text.lower())}

    prior_tokens = tokens(prior)
    vision_tokens = set()
    for block in vision_blocks:
        vision_tokens |= tokens(block.text)
    if not vision_tokens:
        return OpResult(cost=Cost(duration_ms=timer.ms()))

    overlap = len(vision_tokens & prior_tokens) / len(vision_tokens)
    threshold = float(params.get("min_overlap", 0.5))
    quality = {
        "cross_check_overlap": round(overlap, 3),
        "cross_check_threshold": threshold,
        "cross_check_ok": overlap >= threshold,
    }
    diagnostics = []
    if overlap < threshold:
        diagnostics.append(
            f"vision reading disagrees with the text layer (token overlap {overlap:.2f} < {threshold}); "
            "the text layer is authoritative — review this page"
        )
    return OpResult(
        signals={"cross_check_overlap": round(overlap, 3)},
        cost=Cost(duration_ms=timer.ms()),
        diagnostics=diagnostics,
        metadata=quality,
    )
