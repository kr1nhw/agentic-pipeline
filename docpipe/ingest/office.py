"""PPTX and DOCX ingest.

Neither format has pages in the PDF sense, and the decision below is deliberate
rather than incidental:

* **PPTX** — the unit is a slide, which *is* a real presentation page. When a
  slide is a single full-bleed picture (the shape most decks exported from a
  design tool take) that picture IS the page render, at full fidelity. Otherwise
  there is no rasteriser: we extract text, tables and pictures structurally and
  let the router decide, rather than compositing an approximation and asking a
  vision model to read a layout that never existed.
* **DOCX** — the unit is a flow section delimited by headings, capped in length.
  Word's pagination depends on the renderer, so inventing page numbers would be
  inventing structure. A section that is mostly one image exposes that image as
  its raster, which keeps the vision path available for scanned content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Collection, Any, Iterator

from docpipe.api.types import FormatContext, PageSignals, UnitSeed
from docpipe.ingest.base import BaseSession

PPTX_MIME = b"PK\x03\x04"
DOCX_MIME = b"PK\x03\x04"

PPTX_EXTENSIONS = (".pptx", ".pptm")
DOCX_EXTENSIONS = (".docx", ".docm")

#: A section longer than this is split, so one enormous DOCX still streams.
DOCX_SECTION_CHAR_CAP = 4000

#: English Metric Units per point: python-pptx geometry is in EMU.
EMU_PER_POINT = 12700

#: A slide is treated as "picture-backed" above this coverage.
FULL_BLEED_FRACTION = 0.85


def _zip_has(head: bytes, member: bytes) -> bool:
    """Container peek: DOCX and PPTX are both ZIP, distinguished by their parts."""
    return member in head


# ─────────────────────────────────────────────────────────────────────────────
# PPTX
# ─────────────────────────────────────────────────────────────────────────────


class PptxSession(BaseSession):
    def __init__(self, path: Path, doc_id: str, config: Any, work_dir: Path) -> None:
        super().__init__(path=path, doc_id=doc_id, format_id="pptx", config=config, work_dir=work_dir)
        from pptx import Presentation

        self._presentation = Presentation(str(path))
        self._units = len(self._presentation.slides)

    def close(self) -> None:
        return None

    def _slide(self, page_no: int):
        return self._presentation.slides[page_no - 1]

    def slide_shapes(self, page_no: int) -> list[dict[str, Any]]:
        """Shapes in document order, with geometry in slide coordinates."""
        shapes: list[dict[str, Any]] = []
        for shape in self._slide(page_no).shapes:
            try:
                left, top = int(shape.left or 0), int(shape.top or 0)
                width, height = int(shape.width or 0), int(shape.height or 0)
            except (TypeError, ValueError):
                left = top = width = height = 0
            entry: dict[str, Any] = {
                "name": shape.name,
                "shape_type": str(shape.shape_type),
                "left": left,
                "top": top,
                "width": width,
                "height": height,
                "text": "",
                "has_table": bool(getattr(shape, "has_table", False)),
                "is_picture": shape.shape_type is not None and "PICTURE" in str(shape.shape_type),
            }
            if shape.has_text_frame:
                entry["text"] = "\n".join(
                    paragraph.text for paragraph in shape.text_frame.paragraphs
                ).strip()
            shapes.append(entry)
        return shapes

    def slide_text(self, page_no: int) -> str:
        parts = [s["text"] for s in self.slide_shapes(page_no) if s["text"]]
        return "\n".join(parts)

    def slide_dimensions(self, page_no: int) -> tuple[float, float]:
        """Slide size in *points*, not EMU.

        python-pptx reports geometry in English Metric Units (914400 per inch), and
        publishing those as page_width made a slide look 12.7 million points wide.
        Everything in docpipe speaks points, so the conversion happens here, once.
        """
        presentation = self._presentation
        width = float(presentation.slide_width or 0) / EMU_PER_POINT
        height = float(presentation.slide_height or 0) / EMU_PER_POINT
        return round(width, 2), round(height, 2)

    def enumerate_units(self, pages: Collection[int] | None = None) -> Iterator[UnitSeed]:
        wanted = None if pages is None else sorted({int(page) for page in pages})
        for page_no in wanted if wanted is not None else range(1, self._units + 1):
            if page_no < 1 or page_no > self._units:
                continue
            shapes = self.slide_shapes(page_no)
            text = "\n".join(s["text"] for s in shapes if s["text"])
            width, height = self.slide_dimensions(page_no)
            slide_area = float(width * height) or 1.0
            picture_area = sum(s["width"] * s["height"] for s in shapes if s["is_picture"])
            has_table = any(s["has_table"] for s in shapes)
            signals = PageSignals(
                text_layer_chars=len(text.strip()),
                n_images=sum(1 for s in shapes if s["is_picture"]),
                image_area_frac=round(min(1.0, picture_area / slide_area), 4),
                has_table_candidate=has_table,
                n_table_candidates=sum(1 for s in shapes if s["has_table"]),
                page_width=width,
                page_height=height,
                layout_hint="slide",
                text_sample=text.strip()[:220] or None,
            )
            yield UnitSeed(page_no=page_no, kind="slide", signals=signals, text_layer=text)

    def deep_signals(self, page_no: int) -> dict[str, Any]:
        shapes = self.slide_shapes(page_no)
        return {
            "n_shapes": len(shapes),
            "n_text_shapes": sum(1 for s in shapes if s["text"]),
            "n_pictures": sum(1 for s in shapes if s["is_picture"]),
            "n_tables": sum(1 for s in shapes if s["has_table"]),
            "shape_types": sorted({s["shape_type"] for s in shapes}),
        }

    def full_bleed_picture(self, page_no: int) -> Path | None:
        """The slide's own picture, when the slide is fundamentally that picture.

        This is the one Office case where the "page image" is not an
        approximation: it is the artwork the author placed.
        """
        shapes = self.slide_shapes(page_no)
        pictures = [s for s in shapes if s["is_picture"]]
        if len(pictures) != 1:
            return None
        width, height = self.slide_dimensions(page_no)
        slide_area = float(width * height) or 1.0
        picture = pictures[0]
        if picture["width"] * picture["height"] / slide_area < FULL_BLEED_FRACTION:
            return None
        return self._extract_picture(page_no, picture["name"])

    def _extract_picture(self, page_no: int, name: str) -> Path | None:
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        for shape in self._slide(page_no).shapes:
            if shape.name != name:
                continue
            if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
                return None
            image = shape.image
            suffix = "." + (image.ext or "png")
            target = self.config.work_dir / "media" / self.doc_id / f"p{page_no:05d}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(image.blob)
            return target
        return None

    def render(self, page_no: int, dpi: int) -> Path:
        picture = self.full_bleed_picture(page_no)
        if picture is None:
            from docpipe.api.types import OpError

            raise OpError(
                f"slide {page_no} is not picture-backed; this image has no slide renderer, "
                "so use the structural operators (pptx_slide_text, pptx_tables) instead"
            )
        return picture


# ─────────────────────────────────────────────────────────────────────────────
# DOCX
# ─────────────────────────────────────────────────────────────────────────────


class DocxSession(BaseSession):
    def __init__(self, path: Path, doc_id: str, config: Any, work_dir: Path) -> None:
        super().__init__(path=path, doc_id=doc_id, format_id="docx", config=config, work_dir=work_dir)
        import docx

        self._document = docx.Document(str(path))
        self._sections: list[dict[str, Any]] | None = None

    def close(self) -> None:
        return None

    def units(self) -> int:
        """Unit count for a DOCX is the number of flow sections.

        It cannot be known before the body is split, so it is computed on demand
        rather than left at the base class's zero — a zero here silently produced
        an empty window and a document that extracted nothing at all.
        """
        return len(self.sections())

    # ── sections ────────────────────────────────────────────────────────────

    def sections(self) -> list[dict[str, Any]]:
        """Split the body into heading-delimited, length-capped flow sections.

        Recomputed once and cached: DOCX has no page index, so this is the unit
        definition and it must be stable across a resume.
        """
        if self._sections is not None:
            return self._sections

        from docx.table import Table
        from docx.text.paragraph import Paragraph

        body = self._document.element.body
        sections: list[dict[str, Any]] = []
        current: dict[str, Any] = {"heading": None, "paragraphs": [], "tables": [], "chars": 0}
        #: Paragraphs whose text this pass already accounted for, keyed by their
        #: position in the tree. The recovery sweep below reads everything *not*
        #: in here.
        #:
        #: Keyed by XPath and NOT by `id(element)`, which is what it was first:
        #: lxml builds a new proxy object for an element on each access, so the
        #: identity of the same paragraph changes between passes, and a freed
        #: proxy's address can be reused by a different element. The effect was
        #: content both duplicated (a paragraph read twice) and dropped (a
        #: paragraph wrongly considered already read) — measured by the evaluator
        #: as 9% text recall on a real Word file, on a reader whose own text layer
        #: held every character.
        consumed: set[str] = set()

        def key(element: Any) -> str:
            try:
                return element.getroottree().getpath(element)
            except Exception:  # noqa: BLE001 - a detached element has no path
                return ""

        def flush() -> None:
            if current["paragraphs"] or current["tables"]:
                sections.append(dict(current))

        for child in body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = Paragraph(child, self._document)
                text = paragraph.text.strip()
                style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
                if style.startswith("heading") and text:
                    flush()
                    current = {"heading": text, "paragraphs": [], "tables": [], "chars": 0}
                    continue
                if text:
                    current["paragraphs"].append({"text": text, "style": style})
                    current["chars"] += len(text)
                    consumed.add(key(child))
                    if current["chars"] >= DOCX_SECTION_CHAR_CAP:
                        flush()
                        current = {"heading": current["heading"], "paragraphs": [], "tables": [], "chars": 0}
            elif tag == "tbl":
                try:
                    table = Table(child, self._document)
                    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                except Exception:  # noqa: BLE001 - a malformed table is a recovery case, not a crash
                    rows = []
                current["tables"].append(rows)
                current["chars"] += sum(len(cell) for row in rows for cell in row)
                if rows:
                    # `cell.text` covers these paragraphs, so the sweep must not
                    # read them a second time.
                    for inner in child.iter():
                        if inner.tag.rsplit("}", 1)[-1] == "p":
                            consumed.add(key(inner))
        flush()

        # ── coverage backstop ───────────────────────────────────────────────
        # python-docx models well-formed documents: body-level paragraphs, and
        # tables that have rows. Text stored anywhere else is invisible to it —
        # a table whose cells exist but whose `w:tr` rows do not, a paragraph
        # nested inside a cell, a text box. In every one of those cases the text
        # is in the file and in the text layer while reaching no block at all,
        # and the gate then reports a table that was "detected but not
        # extracted" from a document that in fact has fifteen characters of text.
        #
        # Structure may be downgraded; content may not be dropped.
        recovered: list[str] = []
        for element in body.iter():
            if element.tag.rsplit("}", 1)[-1] != "p" or key(element) in consumed:
                continue
            try:
                text = Paragraph(element, self._document).text.strip()
            except Exception:  # noqa: BLE001
                continue
            if text and text not in recovered:
                recovered.append(text)
        if recovered:
            sections.append(
                {
                    "heading": None,
                    "paragraphs": [{"text": text, "style": "recovered"} for text in recovered],
                    "tables": [],
                    "chars": sum(len(text) for text in recovered),
                    "recovered": True,
                }
            )
        self._sections = sections or [{"heading": None, "paragraphs": [], "tables": [], "chars": 0}]
        self._units = len(self._sections)
        return self._sections

    def section_text(self, page_no: int) -> str:
        section = self.sections()[page_no - 1]
        parts: list[str] = []
        if section["heading"]:
            parts.append(section["heading"])
        parts.extend(p["text"] for p in section["paragraphs"])
        for table in section["tables"]:
            parts.append("\n".join(" | ".join(row) for row in table))
        return "\n".join(parts)

    def enumerate_units(self, pages: Collection[int] | None = None) -> Iterator[UnitSeed]:
        wanted = None if pages is None else {int(page) for page in pages}
        for index, section in enumerate(self.sections(), start=1):
            if wanted is not None and index not in wanted:
                continue
            text = self.section_text(index)
            signals = PageSignals(
                text_layer_chars=len(text.strip()),
                n_table_candidates=len(section["tables"]),
                has_table_candidate=bool(section["tables"]),
                layout_hint="flow",
                text_sample=text.strip()[:220] or None,
            )
            yield UnitSeed(
                page_no=index,
                kind="docx_flow",
                signals=signals,
                text_layer=text,
                payload={"heading": section["heading"], "tables": len(section["tables"])},
            )

    def deep_signals(self, page_no: int) -> dict[str, Any]:
        section = self.sections()[page_no - 1]
        return {
            "heading": section["heading"],
            "n_paragraphs": len(section["paragraphs"]),
            "n_tables": len(section["tables"]),
            "styles": sorted({p["style"] for p in section["paragraphs"] if p["style"]})[:10],
        }

    def render(self, page_no: int, dpi: int) -> Path:
        """Expose the section's first inline image, if it is an image-heavy section.

        There is no Word renderer here, so this is the only honest page image a
        DOCX can offer a vision model — and it is the right one for a scanned or
        image-only document.
        """
        from docpipe.api.types import OpError

        section = self.sections()[page_no - 1]
        section_chars = section["chars"]
        if section_chars <= 200:
            for rel_id, rel in self._document.part.rels.items():
                if "image" not in rel.reltype:
                    continue
                blob = rel.target_part.blob
                suffix = Path(str(rel.target_ref)).suffix or ".png"
                target = self.config.work_dir / "media" / self.doc_id / f"p{page_no:05d}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(blob)
                return target
        raise OpError(
            f"docx section {page_no} is text-backed ({section_chars} chars); there is no Word "
            "renderer, so use docx_paragraphs_styled / docx_tables instead"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Format plugin surface
# ─────────────────────────────────────────────────────────────────────────────


def match_pptx(head: bytes, path: str) -> float:
    lowered = path.lower()
    if lowered.endswith(PPTX_EXTENSIONS):
        return 1.0
    if head.startswith(PPTX_MIME) and _zip_has(head, b"ppt/"):
        return 0.9
    return 0.0


def match_docx(head: bytes, path: str) -> float:
    lowered = path.lower()
    if lowered.endswith(DOCX_EXTENSIONS):
        return 1.0
    if head.startswith(DOCX_MIME) and _zip_has(head, b"word/"):
        return 0.9
    return 0.0


def open_pptx(path: Path, doc_id: str, config: Any) -> PptxSession:
    return PptxSession(path=path, doc_id=doc_id, config=config, work_dir=Path(config.work_dir))


def open_docx(path: Path, doc_id: str, config: Any) -> DocxSession:
    return DocxSession(path=path, doc_id=doc_id, config=config, work_dir=Path(config.work_dir))


def enumerate_pptx(ctx: FormatContext) -> Iterator[UnitSeed]:
    session = PptxSession(ctx.path, ctx.doc_id, ctx.scratch.get("config"), ctx.work_dir)
    yield from session.enumerate_units()


def enumerate_docx(ctx: FormatContext) -> Iterator[UnitSeed]:
    session = DocxSession(ctx.path, ctx.doc_id, ctx.scratch.get("config"), ctx.work_dir)
    yield from session.enumerate_units()
