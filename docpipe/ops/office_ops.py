"""Office operators: PPTX and DOCX structure, extracted from the file itself.

python-pptx and python-docx read the *authored* structure — shape order, text
frames, style names, real table cells. For these formats that is strictly better
than any visual reconstruction, which is why the Office path never rasterises
unless a slide is literally one picture.
"""

from __future__ import annotations

from typing import Any

from docpipe.api.types import BBox, Block, Cost, ImagePayload, OpContext, OpResult, Timer

#: PowerPoint style names that mean "this is a heading".
PPTX_HEADING_TOKENS = ("title", "headline", "heading")

#: Word style names map to heading levels through their trailing digit.
DOCX_HEADING_PREFIX = "heading "

#: EMU per point: pptx geometry is in EMU.
EMU_PER_POINT = 12700


def _emu_to_points(value: int) -> float:
    return round(value / EMU_PER_POINT, 2)


def op_pptx_slide_text(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Emit a slide's shapes in document order, with geometry in points.

    Document order is the z-order the author stacked, which is the closest thing
    a slide has to reading order. Titles are promoted by shape role, not by
    guessing from position.
    """
    timer = Timer()
    session = ctx.session
    if session is None or not hasattr(session, "slide_shapes"):
        return OpResult(cost=Cost(duration_ms=timer.ms()), diagnostics=["no pptx session"])

    block_geometry = bool(params.get("geometry", True))
    shapes = session.slide_shapes(ctx.state.page_no)
    blocks: list[Block] = []
    for shape in shapes:
        text = (shape.get("text") or "").strip()
        if not text:
            continue
        name = str(shape.get("name", "")).lower()
        is_title = any(token in name for token in PPTX_HEADING_TOKENS)
        bbox = None
        if block_geometry and shape.get("width"):
            x0 = _emu_to_points(shape["left"])
            y0 = _emu_to_points(shape["top"])
            bbox = BBox(
                x0=x0,
                y0=y0,
                x1=x0 + _emu_to_points(shape["width"]),
                y1=y0 + _emu_to_points(shape["height"]),
            )
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:s{len(blocks)}",
                page_id=ctx.state.page_id,
                order=len(blocks),
                type="heading" if is_title else "paragraph",
                text=text,
                level=1 if is_title else None,
                bbox=bbox,
                source_op="pptx_slide_text",
                confidence=1.0,
                checks={"shape": shape.get("name"), "shape_type": shape.get("shape_type")},
            )
        )
    return OpResult(blocks=blocks, signals={"shapes_with_text": len(blocks)}, cost=Cost(duration_ms=timer.ms()))


def op_pptx_pictures(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Record pictures as figure blocks and extract their bytes.

    On an image-only slide this is the content, not an illustration of it: the
    picture is what the deck actually says, and it is also the raster the vision
    path reads.
    """
    timer = Timer()
    session = ctx.session
    if session is None or not hasattr(session, "slide_shapes"):
        return OpResult(cost=Cost(duration_ms=timer.ms()))

    extract = bool(params.get("extract", True))
    blocks: list[Block] = []
    artifacts: dict[str, str] = {}
    for shape in session.slide_shapes(ctx.state.page_no):
        if not shape.get("is_picture"):
            continue
        uri = None
        if extract:
            try:
                path = session.full_bleed_picture(ctx.state.page_no) if shape.get("width") else None
            except Exception:  # noqa: BLE001 - a missing image must not fail the slide
                path = None
            if path is not None:
                uri = str(path)
                artifacts[f"picture_{len(blocks)}"] = str(path)
        width = _emu_to_points(shape.get("width", 0)) if shape.get("width") else None
        height = _emu_to_points(shape.get("height", 0)) if shape.get("height") else None
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:img{len(blocks)}",
                page_id=ctx.state.page_id,
                order=len(blocks),
                type="figure",
                text="",
                image=ImagePayload(uri=uri, width=int(width) if width else None, height=int(height) if height else None),
                source_op="pptx_pictures",
                confidence=1.0,
                checks={"shape": shape.get("name")},
            )
        )
    return OpResult(blocks=blocks, artifacts=artifacts, signals={"pictures": len(blocks)}, cost=Cost(duration_ms=timer.ms()))


def op_docx_paragraphs_styled(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Emit a DOCX flow section with style names mapped to heading levels.

    Word already knows which paragraphs are headings; reading the style is
    reading the author's own structure rather than inferring it from font size.
    """
    timer = Timer()
    session = ctx.session
    if session is None or not hasattr(session, "sections"):
        return OpResult(cost=Cost(duration_ms=timer.ms()), diagnostics=["no docx session"])

    emit_lists = bool(params.get("lists", True))
    section = session.sections()[ctx.state.page_no - 1]
    blocks: list[Block] = []

    if section.get("heading"):
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:h0",
                page_id=ctx.state.page_id,
                order=0,
                type="heading",
                text=section["heading"],
                level=1,
                source_op="docx_paragraphs_styled",
                confidence=1.0,
            )
        )
    for paragraph in section["paragraphs"]:
        text = paragraph["text"]
        style = paragraph.get("style") or ""
        block_type = "paragraph"
        level = None
        if style.startswith(DOCX_HEADING_PREFIX):
            block_type = "heading"
            try:
                level = int(style[len(DOCX_HEADING_PREFIX) :].strip()) + 1
            except ValueError:
                level = 2
        elif emit_lists and style.startswith("list"):
            block_type = "list"
        blocks.append(
            Block(
                block_id=f"{ctx.state.page_id}:p{len(blocks)}",
                page_id=ctx.state.page_id,
                order=len(blocks),
                type=block_type,  # type: ignore[arg-type]
                text=text,
                level=level,
                source_op="docx_paragraphs_styled",
                confidence=1.0,
                checks={"style": style} if style else {},
            )
        )
    return OpResult(blocks=blocks, signals={"paragraphs": len(section["paragraphs"])}, cost=Cost(duration_ms=timer.ms()))


def op_docx_media(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Expose a DOCX section's images as figure blocks.

    Inline images carry captions in the surrounding text, not in the image, so
    the caption is left to the caption operator rather than invented here.
    """
    timer = Timer()
    session = ctx.session
    if session is None or not hasattr(session, "sections"):
        return OpResult(cost=Cost(duration_ms=timer.ms()))
    try:
        uri = str(session.render(ctx.state.page_no, 0))
    except Exception:  # noqa: BLE001 - no image in this section is the normal case
        return OpResult(cost=Cost(duration_ms=timer.ms()), signals={"images": 0})
    block = Block(
        block_id=f"{ctx.state.page_id}:img0",
        page_id=ctx.state.page_id,
        order=0,
        type="figure",
        text="",
        image=ImagePayload(uri=uri),
        source_op="docx_media",
        confidence=1.0,
    )
    return OpResult(blocks=[block], artifacts={"image_0": uri}, signals={"images": 1}, cost=Cost(duration_ms=timer.ms()))
