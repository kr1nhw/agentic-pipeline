"""Built-in operator registration.

Every operator here goes through :class:`docpipe.api.registry.PluginAPI` — the
same facade a third-party plugin receives. That is not a stylistic choice: if a
builtin needed a privileged path, the plugin API would be a second-class
interface and the "extend it like we do" promise would be false.
"""

from __future__ import annotations

from docpipe.api.registry import PluginAPI
from docpipe.ops import checks, media_ops, office_ops, struct_ops, text_ops

__all__ = ["register_ops"]


def register_ops(api: PluginAPI) -> None:
    # ── free: text ──────────────────────────────────────────────────────────
    api.register_op(
        "pdf_text_layer",
        text_ops.op_pdf_text_layer,
        summary=(
            "Emit the page's own text layer as paragraph blocks. Exact, free, and the ground "
            "truth every vision answer is checked against. Use this first on any page that has "
            "a text layer; never replace it with a model reading."
        ),
        params_schema={
            "preserve_lines": {"type": "boolean", "default": False},
            "drop_running_heads": {"type": "boolean", "default": True},
        },
        produces=("blocks",),
        unit_kinds=("pdf_page",),
    )

    api.register_op(
        "text_passthrough",
        text_ops.op_text_passthrough,
        summary=(
            "Emit a text, Markdown or code unit exactly as ingested, preserving Markdown headings "
            "and code fences. Use for md/txt/code units where the format already implies the "
            "structure."
        ),
        params_schema={},
        produces=("blocks",),
        unit_kinds=("text_chunk", "code_unit"),
    )

    api.register_op(
        "pdf_columns_text",
        text_ops.op_pdf_columns_text,
        summary=(
            "Extract a multi-column page in reading order by separating the columns from line "
            "geometry — no model call. Use it for a page whose only defect is that its text "
            "columns interleave; it cannot help a page whose characters are wrong, and it "
            "cannot separate overlapping text streams."
        ),
        params_schema={},
        cost_class="free",
        produces=("blocks",),
        unit_kinds=("pdf_page",),
    )

    api.register_op(
        "normalize_whitespace",
        text_ops.op_normalize_whitespace,
        summary=(
            "Collapse repeated spaces and blank lines inside blocks already extracted. A cleanup "
            "step: it produces no blocks of its own and must run after an extractor."
        ),
        params_schema={},
        requires=("blocks",),
    )

    api.register_op(
        "pdf_font_heading_map",
        text_ops.op_pdf_font_heading_map,
        summary=(
            "Promote lines set in an oversized font to headings, using the PDF's own font sizes. "
            "Gives a document its heading structure when the text layer alone is a flat wall of "
            "paragraphs."
        ),
        params_schema={
            "size_ratio": {"type": "number", "default": 1.12, "min": 1.0, "max": 3.0},
            "max_levels": {"type": "integer", "default": 4, "min": 1, "max": 6},
            "replace_duplicates": {"type": "boolean", "default": True},
            # pdfminer is correct but slow; pdfium is ~10x faster and not yet
            # reliable on pages where two text runs share a baseline band.
            "backend": {"type": "string", "enum": ["pdfminer", "pdfium"], "default": "pdfminer"},
        },
        cost_class="local",
        produces=("blocks",),
        unit_kinds=("pdf_page",),
    )

    # ── local: structure ────────────────────────────────────────────────────
    api.register_op(
        "pdf_tables",
        struct_ops.op_pdf_tables,
        summary=(
            "Extract tables with pdfplumber. strategy=lines trusts ruling lines (precise, finds "
            "nothing when a table has none); strategy=text infers columns from whitespace "
            "(recovers borderless tables, occasionally over-splits). Rejects tables that come out "
            "mostly empty, so a silent failure becomes a visible gate failure."
        ),
        params_schema={
            "strategy": {"type": "string", "enum": ["lines", "text"], "default": "lines"},
            "min_rows": {"type": "integer", "default": 2, "min": 1, "max": 100},
            "min_cols": {"type": "integer", "default": 2, "min": 1, "max": 100},
            "header_rows": {"type": "integer", "default": 1, "min": 0, "max": 10},
            "max_empty_frac": {"type": "number", "default": 0.6, "min": 0.0, "max": 1.0},
            # Above this many vector objects a page is a figure, and table
            # detection is meaningless and ruinously slow. 0 disables the guard.
            "max_vector_objects": {"type": "integer", "default": 20000, "min": 0},
        },
        cost_class="local",
        requires=("session",),
        produces=("blocks", "table"),
        unit_kinds=("pdf_page",),
    )

    api.register_op(
        "office_tables",
        struct_ops.op_office_tables,
        summary=(
            "Emit tables that a PPTX or DOCX stores as real table structures. Exact: the file "
            "already knows the cells. Prefer this over any vision table extraction for Office "
            "formats."
        ),
        params_schema={},
        cost_class="local",
        produces=("blocks", "table"),
        unit_kinds=("slide", "docx_flow"),
    )

    api.register_op(
        "reading_order_v1",
        struct_ops.op_reading_order,
        summary=(
            "Reorder the page's blocks into reading order, detecting multi-column layouts from "
            "block geometry. Run after extracting blocks and before assembly: a naive top-to-bottom "
            "sort interleaves two-column papers."
        ),
        params_schema={},
        cost_class="local",
        requires=("blocks",),
    )

    api.register_op(
        "caption_blocks",
        struct_ops.op_caption_blocks,
        summary=(
            "Mark lines that begin like a caption ('Table 3:', 'Figure 2', '图 1') as caption "
            "blocks, superseding the paragraph they came from. Identifies captions by their own "
            "text, never by guessing from proximity to a picture."
        ),
        params_schema={},
        cost_class="local",
        requires=("blocks",),
    )

    api.register_op(
        "pptx_slide_text",
        office_ops.op_pptx_slide_text,
        summary=(
            "Emit a slide's shapes in stacking order with geometry, promoting title placeholders to "
            "headings. The structural operator for text slides; no rasterisation needed."
        ),
        params_schema={"geometry": {"type": "boolean", "default": True}},
        cost_class="local",
        produces=("blocks",),
        unit_kinds=("slide",),
    )

    api.register_op(
        "pptx_pictures",
        office_ops.op_pptx_pictures,
        summary=(
            "Record a slide's pictures as figure blocks and extract their bytes. On an image-only "
            "slide this is the content itself and the raster the vision path reads."
        ),
        params_schema={"extract": {"type": "boolean", "default": True}},
        cost_class="local",
        produces=("blocks", "figure"),
        unit_kinds=("slide",),
    )

    api.register_op(
        "docx_paragraphs_styled",
        office_ops.op_docx_paragraphs_styled,
        summary=(
            "Emit a DOCX flow section with Word style names mapped to heading levels and list "
            "blocks. Uses the author's own structure rather than inferring it."
        ),
        params_schema={"lists": {"type": "boolean", "default": True}},
        cost_class="local",
        produces=("blocks",),
        unit_kinds=("docx_flow",),
    )

    api.register_op(
        "docx_media",
        office_ops.op_docx_media,
        summary=(
            "Expose a DOCX section's image when the section is image-backed. Keeps the vision path "
            "available for scanned Word documents without pretending Word has pages."
        ),
        params_schema={},
        cost_class="local",
        produces=("blocks", "figure"),
        unit_kinds=("docx_flow",),
    )

    # ── local: raster ───────────────────────────────────────────────────────
    api.register_op(
        "page_raster",
        media_ops.op_page_raster,
        summary=(
            "Render this unit to a PNG and publish it as the 'raster' artifact, which every vision "
            "operator requires. Refuses to render below the legibility floor (110 dpi) because "
            "under-resolved pages make the model invent content."
        ),
        params_schema={
            "dpi": {"type": "integer", "default": 150, "min": 110, "max": 600},
            "for_tables": {"type": "boolean", "default": False},
            "force": {"type": "boolean", "default": False},
        },
        cost_class="local",
        produces=("raster",),
    )

    api.register_op(
        "img_probe",
        media_ops.op_img_probe,
        summary="Report cheap statistics for a standalone image unit (size, ink fraction, aspect ratio).",
        params_schema={},
        cost_class="local",
        unit_kinds=("image",),
    )

    # ── vl: the gated tier ──────────────────────────────────────────────────
    api.register_op(
        "vl_page_classify",
        media_ops.op_vl_page_classify,
        summary=(
            "Ask the vision model what kind of page this is (role, tables, figures, text quality). "
            "Cheap, thinking is off by default, and the answer is advisory: it steers routing and "
            "never becomes extracted content."
        ),
        params_schema={
            "dpi": {"type": "integer", "default": 150, "min": 110, "max": 600},
            "thinking": {"type": "boolean", "default": False},
            "max_tokens": {"type": "integer", "default": 512, "min": 64, "max": 4096},
            "question": {"type": "string"},
        },
        cost_class="vl",
        requires=("raster",),
    )

    api.register_op(
        "vl_extract",
        media_ops.op_vl_extract,
        summary=(
            "Transcribe the page with the vision model: OCR, layout, tables, formulas and figure "
            "descriptions. The escalation path — reach it only after deterministic operators have "
            "failed, because it costs tokens and replaces their output rather than adding to it."
        ),
        params_schema={
            "tasks": {
                "type": "array",
                "default": ["ocr", "layout"],
                "items": {"type": "string", "enum": ["ocr", "layout", "tables", "figures", "formulas", "describe"]},
            },
            "dpi": {"type": "integer", "default": 150, "min": 110, "max": 600},
            "thinking": {"type": "boolean", "default": False},
            "max_tokens": {"type": "integer", "default": 4000, "min": 256, "max": 32000},
            "replace": {"type": "boolean", "default": True},
        },
        cost_class="vl",
        requires=("raster",),
        produces=("blocks",),
    )

    api.register_op(
        "vl_cross_check",
        media_ops.op_vl_cross_check,
        summary=(
            "Compare a vision transcription against the text layer and record the disagreement. "
            "Run it whenever both exist: the text layer is authoritative, and a contradiction "
            "means the page needs review."
        ),
        params_schema={
            "min_overlap": {"type": "number", "default": 0.5, "min": 0.0, "max": 1.0},
        },
        cost_class="free",
        requires=("blocks",),
    )

    # ── quality ─────────────────────────────────────────────────────────────
    api.register_op(
        "gate_default_v1",
        checks.op_gate_default,
        summary=(
            "Score the page and report whether it needs escalation (garbled encoding, scanned "
            "image, missing tables, thin or absent text). Run it last: it decides whether the "
            "deterministic result is good enough, and it is what keeps vision spend proportional."
        ),
        params_schema={
            "text_floor_chars": {"type": "integer", "default": 80, "min": 0, "max": 5000},
        },
        cost_class="free",
    )

    api.register_op(
        "chunk_for_memory",
        checks.op_chunk_for_memory,
        summary=(
            "Group the page's blocks into retrieval-sized chunks carrying their heading path and "
            "block ids. Run last, after assembly, because provenance is only available here."
        ),
        params_schema={
            "target_chars": {"type": "integer", "default": 1200, "min": 200, "max": 8000},
            "max_chars": {"type": "integer", "default": 2000, "min": 200, "max": 20000},
            "min_chars": {"type": "integer", "default": 200, "min": 0, "max": 4000},
        },
        cost_class="free",
        requires=("blocks",),
    )
