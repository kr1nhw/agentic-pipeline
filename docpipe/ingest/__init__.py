"""Built-in format registration.

Formats are matched on *bytes first*: an extension is a claim, a magic number is
evidence. The two agree most of the time, and the times they disagree — a `.pdf`
that is really a ZIP, a Word file renamed to `.doc` — are exactly the times a
pipeline chosen from the extension would waste a run.
"""

from __future__ import annotations

from docpipe.api.registry import PluginAPI
from docpipe.ingest import image, office, pdf, textlike

__all__ = ["register_formats"]

#: Formats whose default operators are used when no archetype matches at all.
DEFAULT_OPS = {
    "pdf": ("pdf_text_layer", "pdf_tables", "reading_order_v1"),
    "pptx": ("pptx_slide_text", "office_tables", "pptx_pictures"),
    "docx": ("docx_paragraphs_styled", "office_tables", "docx_media"),
    "text": ("text_passthrough",),
    "code": ("text_passthrough",),
    "image": ("img_probe",),
}


def register_formats(api: PluginAPI) -> None:
    api.register_format(
        "pdf",
        match=pdf.match,
        enumerate_units=pdf.enumerate_units,
        session_factory=pdf.open_session,
        kind="pdf_page",
        extensions=pdf.PDF_EXTENSIONS,
        default_ops=DEFAULT_OPS["pdf"],
        seekable=True,
        streaming=False,
    )

    api.register_format(
        "pptx",
        match=office.match_pptx,
        enumerate_units=office.enumerate_pptx,
        session_factory=office.open_pptx,
        kind="slide",
        extensions=office.PPTX_EXTENSIONS,
        default_ops=DEFAULT_OPS["pptx"],
        seekable=True,
    )

    api.register_format(
        "docx",
        match=office.match_docx,
        enumerate_units=office.enumerate_docx,
        session_factory=office.open_docx,
        kind="docx_flow",
        extensions=office.DOCX_EXTENSIONS,
        default_ops=DEFAULT_OPS["docx"],
        seekable=True,
    )

    # Code before text: both match on extension, and a `.py` file is a code unit.
    api.register_format(
        "code",
        match=textlike.match_code,
        enumerate_units=textlike.enumerate_text,
        session_factory=textlike.open_text,
        kind="code_unit",
        extensions=textlike.CODE_EXTENSIONS,
        default_ops=DEFAULT_OPS["code"],
        seekable=True,
        streaming=True,
    )

    api.register_format(
        "text",
        match=textlike.match_text,
        enumerate_units=textlike.enumerate_text,
        session_factory=textlike.open_text,
        kind="text_chunk",
        extensions=textlike.TEXT_EXTENSIONS,
        default_ops=DEFAULT_OPS["text"],
        seekable=True,
        streaming=True,
    )

    api.register_format(
        "image",
        match=image.match_image,
        enumerate_units=image.enumerate_image,
        session_factory=image.open_image,
        kind="image",
        extensions=image.IMAGE_EXTENSIONS,
        default_ops=DEFAULT_OPS["image"],
        seekable=True,
    )
