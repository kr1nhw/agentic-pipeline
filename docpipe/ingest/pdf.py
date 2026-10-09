"""PDF ingest: text layer, cheap signals, deep signals and rasterisation.

Two text backends, in preference order:

1. ``pdftotext -layout`` when the binary exists. Layout mode preserves the visual
   line structure, which is what a reading-order pass wants, and one invocation
   covers a whole window of pages instead of one process per page.
2. PDFium (pypdfium2), always available because it ships in the wheel.

The text layer is *ground truth*: every vision answer is checked against it when
it exists, because the model demonstrably invents content on under-resolved
rasters.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator

from docpipe.api.types import FormatContext, OpError, PageSignals, UnitSeed
from docpipe.ingest.base import BaseSession, doc_id_for

#: PDFium is not thread-safe. Concurrent access to its documents corrupts shared
#: library state, and the symptom is not an exception in the offending thread —
#: it is the whole process dying. That is exactly what happened to a 1428-page
#: run at `--workers 8`: every page in the failing window extracted perfectly in a
#: single-threaded loop, and the run still died.
#:
#: Every PDFium call therefore goes through this lock. Parallelism is not lost:
#: the expensive per-page work — `pdftotext`, pdfplumber, table detection and
#: every model call — happens outside it, and those are where the wall time is.
_PDFIUM_LOCK = threading.Lock()


#: Markers that mean the text layer is present but useless. These are the exact
#: failure modes this corpus produced: CID placeholders where a font's ToUnicode
#: map is missing, and replacement characters from a bad decode.
CID_PATTERN = re.compile(r"\(cid:\d+\)")
CTRL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

PDF_EXTENSIONS = (".pdf",)


def _pdftotext() -> str | None:
    return shutil.which("pdftotext")


#: Fraction of tokens that must look like words before a page's text is believed.
#:
#: Measured, not guessed. On the four documents whose text layer is unembedded and
#: carries no unicode maps — the only group the font metadata cannot clear:
#:
#:   topology paper   (1950s scan)  word shape 0.47   text GARBLED, unusable
#:   1982 survey                    word shape 0.92   text correct, rare substitutions
#:   Congressional Record           word shape 0.93   text correct
#:   Federal Register               word shape 0.77   text correct
#:
#: The threshold sits between the one document that must be refused and the three
#: that must not, and the margin is wide enough to be a rule rather than a fit.
WORD_SHAPE_FLOOR = 0.60

_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_WORD_EDGE = ".,;:!?()[]{}<>\"'`/*_+=|\\"


def text_word_shape(text: str) -> float:
    """Fraction of whitespace-separated tokens that are made of letters.

    This is the second opinion on a text layer. Font metadata can only say how
    the characters *should* have been decoded: a font with no ToUnicode CMap and
    no standard encoding might have produced the text by guessing from glyph
    names. The text itself says whether that guess landed. Prose is built of
    letter-only tokens; a layer decoded from glyph names is built of fragments
    like ``~'(R)``, ``R tl]`` and ``U{Ry``, which are letters padded with the
    symbols whose glyph names happened to be nearby.

    Deliberately language-neutral: it asks for the shape of a word, not for a
    dictionary, so a page in a language this was never tested on is not punished
    for it. A page with fewer than twenty tokens returns 1.0 — too little
    evidence to convict, and the font metadata on its own is not enough.
    """
    tokens = [token for token in text.split() if any(ch.isalnum() for ch in token)]
    if len(tokens) < 20:
        return 1.0
    good = 0
    for token in tokens:
        core = token.strip(_WORD_EDGE)
        if len(core) >= 2 and all(ch in _WORD_CHARS or ch in "-'" for ch in core):
            good += 1
    return good / len(tokens)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def page_signals(text: str, width: float, height: float) -> PageSignals:
    """Tier-1 signals: free, per page, no extra I/O beyond the text layer."""
    sample = text.strip()[:220]
    suspect = bool(CID_PATTERN.search(text)) or text.count("\ufffd") > 3
    hint = None
    if width and height:
        ratio = width / height
        if 1.6 <= ratio <= 1.9:
            hint = "slide"
        elif ratio >= 1.35:
            hint = "landscape"
    return PageSignals(
        text_layer_chars=len(text.strip()),
        encoding_suspect=suspect,
        page_width=round(width, 2),
        page_height=round(height, 2),
        layout_hint=hint,
        text_sample=sample or None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Column geometry
# ─────────────────────────────────────────────────────────────────────────────

#: A page needs at least this many lines before a column split is meaningful.
MIN_LINES_FOR_COLUMNS = 8

#: Each side of the boundary must carry at least this share of the lines.
MIN_COLUMN_SIDE_FRACTION = 0.2

#: At most this share of lines may cross the boundary for it to be a gutter.
MAX_CROSSING_FRACTION = 0.12


def detect_columns(
    lines: list[dict[str, Any]],
    *,
    min_lines: int = MIN_LINES_FOR_COLUMNS,
    min_side_fraction: float = MIN_COLUMN_SIDE_FRACTION,
    max_crossing_fraction: float = MAX_CROSSING_FRACTION,
) -> tuple[float | None, dict[str, Any]]:
    """Locate the column gutter: the x boundary that the fewest lines cross.

    Three other criteria were tried against a real two-column ACL page whose
    columns are visually obvious (x = 71..290 and x = 306..526) and each failed:

    * a band no character occupies — one full-width caption crosses the gutter and
      fills it;
    * the largest gap in line *starts* — lines beginning at varying x inside a
      column (indented equations, list items) put the boundary mid-column;
    * the band with the sparsest occupancy — a few lines ending a little past the
      column edge leave the gutter around 80 % occupied, so every occupancy
      threshold is arbitrary.

    Crossing count has no threshold to tune into a corner: a boundary inside a
    column is crossed by *every* line of that column, while a boundary in the
    gutter is crossed only by the occasional overlong line. The minimum is sharp
    rather than marginal, and a page that cannot produce it says so instead of
    guessing.

    Pure geometry, no session and no I/O, so the criteria above can be tested
    directly rather than through a pipeline run.
    """
    usable = [line for line in lines if str(line.get("text", "")).strip()]
    if len(usable) < min_lines:
        return None, {"reason": f"only {len(usable)} lines", "columns": 1}

    left = min(line["x0"] for line in usable)
    right = max(line["x1"] for line in usable)
    width = right - left
    if width <= 0:
        return None, {"reason": "zero text width", "columns": 1}

    best: tuple[int, float, int, int] | None = None
    for fraction in range(25, 76):
        boundary = left + fraction / 100.0 * width
        n_left = sum(1 for line in usable if line["x1"] <= boundary)
        n_right = sum(1 for line in usable if line["x0"] >= boundary)
        if min(n_left, n_right) < len(usable) * min_side_fraction:
            continue
        crossings = sum(1 for line in usable if line["x0"] < boundary < line["x1"])
        if best is None or crossings < best[0]:
            best = (crossings, boundary, n_left, n_right)

    if best is None:
        return None, {"reason": "no boundary leaves text on both sides", "columns": 1}

    crossings, boundary, n_left, n_right = best
    allowed = max(1, int(len(usable) * max_crossing_fraction))
    if crossings > allowed:
        return None, {
            "reason": f"{crossings} of {len(usable)} lines cross the best boundary (limit {allowed})",
            "columns": 1,
            "crossings": crossings,
        }
    return boundary, {
        "columns": 2,
        "crossings": crossings,
        "left_lines": n_left,
        "right_lines": n_right,
        "text_width": round(width, 1),
    }


def assign_columns(lines: list[dict[str, Any]], boundary: float | None) -> None:
    """Tag each line with its column: 0 spans the gutter, 1 left, 2 right."""
    if boundary is None:
        for line in lines:
            line["column"] = 1
        return
    for line in lines:
        if line["x0"] < boundary < line["x1"]:
            line["column"] = 0
        elif (line["x0"] + line["x1"]) / 2.0 <= boundary:
            line["column"] = 1
        else:
            line["column"] = 2


class PdfSession(BaseSession):
    """One open PDF, reused for every page this worker touches."""

    def __init__(self, path: Path, doc_id: str, config: Any, work_dir: Path) -> None:
        super().__init__(path=path, doc_id=doc_id, format_id="pdf", config=config, work_dir=work_dir)
        import pypdfium2 as pdfium

        self._pdfium = pdfium
        with _PDFIUM_LOCK:
            self._pdf = pdfium.PdfDocument(str(path))
        self._units = len(self._pdf)
        self._text_backend = "pdftotext" if _pdftotext() else "pdfium"
        #: One pdfplumber handle per document, with a bounded page-object window.
        self._plumber: Any = None
        self._plumber_pdf: Any = None
        self._plumber_page_count = 0
        self._plumber_seen: list[int] = []
        #: Cheap per-page facts, computed once and reused by the ops that ask.
        self._profiles: dict[int, dict[str, Any]] = {}
        self._layouts: dict[int, dict[str, Any]] = {}
        #: Pages where pdfminer blew its wall-clock budget.
        self._pdfminer_timeouts: set[int] = set()
        self._font_report: dict[str, Any] | None = None
        # Window cache: one pdftotext call covers many pages.
        self._text_window: tuple[int, int] | None = None
        self._text_pages: list[str] = []

    # ── lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        self._close_plumber()
        try:
            with _PDFIUM_LOCK:
                self._pdf.close()
        except Exception:  # noqa: BLE001 - closing must never raise during teardown
            pass

    # ── text ────────────────────────────────────────────────────────────────

    @property
    def text_backend(self) -> str:
        return self._text_backend

    def page_text(self, page_no: int) -> str:
        if self._text_backend == "pdftotext":
            return self._pdftotext_page(page_no)
        return self._pdfium_page_text(page_no)

    def _pdftotext_window(self, start: int, end: int) -> list[str]:
        window = (start, end)
        if self._text_window == window:
            return self._text_pages
        binary = _pdftotext()
        if binary is None:
            self._text_backend = "pdfium"
            return [self._pdfium_page_text(n) for n in range(start, end + 1)]
        args = [
            binary,
            "-layout",
            "-enc",
            "UTF-8",
            "-f",
            str(start),
            "-l",
            str(end),
            str(self.path),
            "-",
        ]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OpError(f"pdftotext failed on {self.path.name}: {exc}") from exc
        if proc.returncode != 0 and not proc.stdout:
            # A single weird page should not lose the whole document.
            self._text_backend = "pdfium"
            return [self._pdfium_page_text(n) for n in range(start, end + 1)]
        text = _decode(proc.stdout)
        parts = text.split("\f")
        if parts and parts[-1].strip() == "":
            parts.pop()
        self._text_window = window
        self._text_pages = parts
        return parts

    def _pdftotext_page(self, page_no: int) -> str:
        window_size = 32
        start = ((page_no - 1) // window_size) * window_size + 1
        end = min(self._units, start + window_size - 1)
        parts = self._pdftotext_window(start, end)
        index = page_no - start
        if 0 <= index < len(parts):
            return parts[index]
        return self._pdfium_page_text(page_no)

    def _pdfium_page_text(self, page_no: int) -> str:
        textpage = None
        with _PDFIUM_LOCK:
            page = self._pdf[page_no - 1]
        try:
            with _PDFIUM_LOCK:
                textpage = page.get_textpage()
                return textpage.get_text_range() or ""
        except Exception as exc:  # noqa: BLE001 - a broken page yields no text, not a crash
            raise OpError(f"text extraction failed on page {page_no}: {exc}") from exc
        finally:
            if textpage is not None:
                try:
                    with _PDFIUM_LOCK:
                        textpage.close()
                except Exception:  # noqa: BLE001
                    pass
            with _PDFIUM_LOCK:
                page.close()

    # ── geometry ────────────────────────────────────────────────────────────

    def page_lines(self, page_no: int) -> list[dict[str, Any]]:
        """Text lines with geometry and TRUE font size, from PDFium alone.

        This is what makes headings affordable. pdfminer's layout analysis costs
        ~250 ms per page; PDFium reports a box and a real font size for every
        character in ~25 ms, and measurably separates a title (17.2pt) from
        headings (12pt) from body text (9pt) on the same page.

        Note where `size` comes from: the font size PDFium reports for the glyph,
        not the height of its bounding box. Box height varies with the glyph — an
        'l' is taller than an 'a' — so a box-height histogram is noise, while a
        font-size histogram is a handful of clean buckets.
        """
        page = self._pdf[page_no - 1]
        textpage = None
        try:
            with _PDFIUM_LOCK:
                textpage = page.get_textpage()
                count = textpage.count_chars()
                get_font_size = _font_size_fn()
                chars: list[dict[str, Any]] = []
                for index in range(count):
                    char = textpage.get_text_range(index, 1)
                    if not char or char in ("\r", "\n", "\x00"):
                        continue
                    left, bottom, right, top = textpage.get_charbox(index)
                    size = 0.0
                    if get_font_size is not None:
                        try:
                            size = float(get_font_size(textpage.raw, index))
                        except Exception:  # noqa: BLE001 - size is an enhancement
                            size = 0.0
                    chars.append(
                        {
                            "char": char,
                            "x0": left,
                            "x1": right,
                            "top": top,
                            "bottom": bottom,
                            "size": size,
                        }
                    )
            return group_chars_into_lines(chars)
        except Exception as exc:  # noqa: BLE001 - geometry is an enhancement, not a requirement
            raise OpError(f"line extraction failed on page {page_no}: {exc}") from exc
        finally:
            if textpage is not None:
                try:
                    with _PDFIUM_LOCK:
                        textpage.close()
                except Exception:  # noqa: BLE001
                    pass
            with _PDFIUM_LOCK:
                page.close()

    def page_size(self, page_no: int) -> tuple[float, float]:
        with _PDFIUM_LOCK:
            page = self._pdf[page_no - 1]
            try:
                return page.get_size()
            finally:
                page.close()

    def enumerate_units(self, pages: Collection[int] | None = None) -> Iterator[UnitSeed]:
        """Stream page seeds, filling the text layer as we go.

        Nothing is rasterised here: the router decides from text, geometry and
        cheap structure whether a page deserves pixels at all.

        `pages` limits the work to those page numbers. Profiling a page costs a
        PDFium object walk, and a large document has to be read by the probe and
        then again by every execution window; without a page filter the same
        357-page issue is profiled three times to answer questions about sixty
        pages and about two hundred.
        """
        wanted = None if pages is None else sorted({int(page) for page in pages})
        for page_no in wanted if wanted is not None else range(1, self._units + 1):
            if page_no < 1 or page_no > self._units:
                continue
            try:
                width, height = self.page_size(page_no)
                text = self.page_text(page_no)
            except OpError as exc:
                yield UnitSeed(
                    page_no=page_no, kind="pdf_page", signals=PageSignals(), payload={"error": str(exc)}
                )
                continue
            signals = page_signals(text, width, height)
            # Layout and text-layer trust are what make routing per PAGE rather
            # than per document possible: one page of a scanned appendix, one page
            # of a two-column insert, and the rest born-digital text are all normal.
            layout = self.page_layout(page_no)
            signals.columns = layout.get("columns", 1)
            signals.text_layer_trust = layout.get("trust", 1.0)
            # The measurement behind a distrust decision travels with the signal,
            # so the gate can report the evidence instead of restating a rule.
            shape = self.font_report().get("word_shape")
            if shape is not None:
                signals.word_shape = shape
            yield UnitSeed(page_no=page_no, kind="pdf_page", signals=signals, text_layer=text)

    # ── text-layer trust and page layout ────────────────────────────────────

    def font_report(self) -> dict[str, Any]:
        """Whether the document's fonts are embedded and carry unicode maps.

        This is the cheapest reliable signal that a text layer came from OCR, and
        it is decisive. A born-digital PDF embeds subset fonts with a ToUnicode
        CMap, so extraction is a dictionary lookup and the characters are exactly
        the author's. An OCR'd scan references a system font by name with no
        unicode map, so extraction guesses glyph-to-character from the font name —
        and that guess is confidently wrong for anything outside plain prose.

        The document that prompted this, a 1950s topology paper, looks fine by every
        other measure: 2447 characters a page, 195 words, nine replacement
        characters. Its text layer renders `T(R)` as `~'(R)`, `R^[1]` as `R tl]`,
        and `∪{R[y] × yS | y ∈ Y}` as `U{Ry x ySI y E Y}`. Nothing in the output
        signals a problem; only the fonts do.

            pdffonts scan.pdf   -> TimesNewRoman  TrueType  no no no
            pdffonts paper.pdf  -> RKVWCA+LinLibertineTI  Type 1  yes yes yes
        """
        if self._font_report is not None:
            return self._font_report

        report: dict[str, Any] = {"total": 0, "embedded": 0, "unicode_mapped": 0, "ocr_like": False, "fonts": []}
        binary = shutil.which("pdffonts")
        if binary:
            try:
                proc = subprocess.run(
                    [binary, str(self.path)], capture_output=True, timeout=60, check=False
                )
                lines = _decode(proc.stdout).splitlines()
                # Column positions come from the HEADER, never from fixed indices.
                #
                # pdffonts omits or inserts columns depending on the document: an
                # `encoding` column appears when fonts differ in encoding, and a
                # `prob` column only for some font types. Reading parts[2] as `emb`
                # therefore read `encoding` instead, marking an ordinary LaTeX paper
                # (`LKQPVO+CMR10 ... Builtin yes yes yes`) as unembedded and sending
                # 15 of its pages to OCR. The header is the only reliable map.
                columns: dict[str, int] = {}
                if len(lines) >= 2:
                    header = lines[0].split()
                    for index, label in enumerate(header):
                        columns.setdefault(label, index)
                emb_at = columns.get("emb", 2)
                uni_at = columns.get("uni", 4)
                for line in lines[2:]:
                    parts = line.split()
                    if len(parts) <= max(emb_at, uni_at):
                        continue
                    name, font_type = parts[0], (parts[1] if len(parts) > 1 else "?")
                    embedded = parts[emb_at]
                    unicode_ok = parts[uni_at]
                    report["total"] += 1
                    report["fonts"].append({"name": name[:40], "type": font_type, "embedded": embedded == "yes", "unicode": unicode_ok == "yes"})
                    if embedded == "yes":
                        report["embedded"] += 1
                    if unicode_ok == "yes":
                        report["unicode_mapped"] += 1
            except (OSError, subprocess.SubprocessError):
                pass

        if report["total"]:
            # A document in which NO font carries a Unicode map has an OCR text
            # layer. That is the whole rule, and it took four attempts to find:
            #
            #   v1  required unembedded AND unmapped, graded by both
            #   v2  graded by the unicode ratio      -> flagged the SQL standard,
            #       whose 15-of-16 EMBEDDED fonts extract flawlessly
            #   v3  graded by embeddedness alone     -> flagged Chucky and the
            #       sigmod tutorial, born-digital, whose unembedded maths fonts
            #       carry almost no text
            #   v4  required BOTH ratios >= 60%      -> still flagged an arXiv
            #       statistics paper at exactly 33/55 unmapped
            #
            # Plus a parsing bug underneath all of them: pdffonts inserts an
            # `encoding` column and a `prob` column depending on the document, and
            # reading fixed indices took `Builtin` for `emb` — which is what made
            # a good LaTeX paper look unembedded. Columns now come from the header.
            #
            # Measured across seven documents whose right answer is known:
            #   topology paper   uni  0/42 =  0%  -> OCR   (text is corrupted)
            #   1980s survey     uni  0/12 =  0%  -> OCR   (text is corrupted)
            #   arXiv stats      uni 22/55 = 40%  -> ok    (extracts correctly)
            #   arXiv maths      uni 22/35 = 63%  -> ok
            #   ACL paper        uni 38/42 = 90%  -> ok
            #   SQL standard     uni 12/16 = 75%  -> ok    (extracts correctly)
            #   Chucky           uni 34/54 = 63%  -> ok
            #
            # Embedding is deliberately not part of it: it looked like the obvious
            # discriminator and it is wrong in both directions, because an embedded
            # font can still lack a map and an unembedded one can still carry the
            # text correctly.
            mapped = report["unicode_mapped"] / report["total"]
            report["unicode_ratio"] = round(mapped, 3)
            report["embedded_ratio"] = round(report["embedded"] / report["total"], 3)
            report["ocr_like"] = bool(mapped < 0.15)
        # The cache guard at the top of this method read `self._font_report` and
        # nothing ever assigned it, so every call ran `pdffonts` again: measured at
        # 943 ms on a 1711-font document, called once per page through
        # `page_layout`, which is roughly five minutes of subprocess on a 357-page
        # issue and the largest single component of that run. The font table is a
        # property of the FILE, so one call is enough.
        self._font_report = report
        return report

    def page_layout(self, page_no: int) -> dict[str, Any]:
        """Column structure, measured from line positions.

        Delegates to :func:`detect_columns`, which explains why crossing count is
        the criterion and the other three are not. Keeping the detector pure means
        its criteria are unit-tested against the real page geometry that defeated
        them in production, rather than being tuned by re-running a corpus.
        """
        cached = self._layouts.get(page_no)
        if cached is not None:
            return cached

        layout: dict[str, Any] = {"columns": 1, "gutter": None, "coverage": 0.0, "profiled": False}
        try:
            lines = [line for line in self.page_lines(page_no) if line["text"].strip()]
            boundary, evidence = detect_columns(lines)
            layout.update({key: value for key, value in evidence.items() if key != "columns"})
            layout["columns"] = evidence.get("columns", 1)
            layout["gutter"] = (round(boundary, 1), round(boundary, 1)) if boundary is not None else None
            if lines:
                left = min(line["x0"] for line in lines)
                right = max(line["x1"] for line in lines)
                with _PDFIUM_LOCK:
                    page = self._pdf[page_no - 1]
                    width = float(page.get_width()) or 1.0
                    page.close()
                layout["coverage"] = round((right - left) / width, 3)
            layout["profiled"] = True
        except Exception as exc:  # noqa: BLE001 - a layout estimate is an optimisation
            layout["error"] = str(exc)
        layout["trust"] = self.text_layer_trust(page_no, layout)
        self._layouts[page_no] = layout
        return layout

    def reading_order(self, page_no: int) -> list[dict[str, Any]]:
        """Lines in reading order, resolving columns when the page has them.

        Full-width elements keep their vertical place, then each column is read top
        to bottom. That is what a person does, and unlike the flat text layer —
        which preserves lines but not the column a line belongs to — the result is
        readable.
        """
        lines = [line for line in self.page_lines(page_no) if line["text"].strip()]
        if not lines:
            return []
        ordered = sorted(lines, key=lambda line: -line["top"])
        layout = self.page_layout(page_no)
        boundary = (layout.get("gutter") or (None, None))[0]
        assign_columns(ordered, boundary if layout.get("columns", 1) >= 2 else None)
        if layout.get("columns", 1) < 2:
            return ordered

        top_band = max(line["top"] for line in ordered)
        bottom_band = min(line["bottom"] for line in ordered)
        height = max(1.0, top_band - bottom_band)

        def sort_key(line: dict[str, Any]) -> tuple[int, float]:
            if line["column"] == 0:
                return (0, (top_band - line["top"]) / height)
            return (line["column"], top_band - line["top"])

        return sorted(ordered, key=sort_key)

    def text_layer_trust(self, page_no: int, layout: dict[str, Any] | None = None) -> float:
        """How much the text layer can be believed, 0..1.

        This is the signal the gate was missing. `gate_default_v1` asks whether the
        text layer is *present and not obviously broken*; it has no opinion on
        whether the characters are the right ones. On an OCR layer they are not,
        and nothing in the text says so.
        """
        report = self.font_report()
        # Deliberately a two-state signal, because every graded version of it was
        # wrong, and each was wrong on a document in this corpus:
        #
        #   version 1  required unembedded AND unmapped, graded by both
        #   version 2  weighted the unicode column   -> flagged all 1428 pages of
        #              the SQL standard, whose 15-of-16 EMBEDDED fonts extract
        #              "TIMEZONE_MINUTE ... 00 to 59" flawlessly
        #   version 3  graded by embeddedness alone  -> flagged Chucky and the
        #              sigmod tutorial, born-digital documents whose many
        #              unembedded maths/graphics fonts carry almost no characters
        #
        # The lesson is that a ratio of FONT COUNTS is not a proxy for how much of
        # the TEXT is unreliable: one system font can carry a whole page, and fifty
        # embedded ones can carry a caption. So this reports distrust only when the
        # evidence is unambiguous — most fonts unembedded *and* most without unicode
        # maps — which is the signature of a scan put through an OCR tool:
        #
        #   topology paper   emb 0/42, uni 0/42    -> distrust  (confirmed by eye)
        #   SQL standard     emb 15/16, uni ~0/16  -> trust
        #   LaTeX paper      emb 20/54, uni 34/54  -> trust
        #   slide tutorial   emb 60/249, uni 189/249 -> trust
        #
        # The 1980s survey was in that list as a second distrust case, and the
        # corpus round of 2024 showed the entry was wrong: its text reads
        # `M. Jarke and J. Koch ... network DBMSs [Dayal and Goodman 1982]`, with
        # an occasional substituted letter, and refusing it sent a readable
        # document down the vision path. The same round produced the Congressional
        # Record: 329 unembedded fonts, none with a unicode map, and text that
        # extracts perfectly, because a font carrying a STANDARD encoding
        # (WinAnsi) has a byte-to-character table that needs no CMap at all.
        #
        # So font metadata is a PROPOSAL, not a verdict. It says the characters
        # might have been inferred; the text says whether the inference landed.
        # Prose is made of words, and a layer decoded from glyph names is not.
        if report.get("ocr_like"):
            shape = text_word_shape(self.page_text(page_no))
            report["word_shape"] = round(shape, 3)
            if shape < WORD_SHAPE_FLOOR:
                return 0.2
            # The fonts cannot vouch for the layer and the text does. The text
            # wins: it is the thing being judged.
            report["ocr_like_text_confirmed"] = False
        return 1.0

    def mark_pdfminer_timeout(self, page_no: int) -> None:
        """Record that pdfminer exceeded its budget on this page.

        Abandoning the work is not the same as avoiding it. The abandoned thread
        keeps running, and the next operator that needs the same parsed page pays
        for it again — measured, that made a slow document *slower* (139s -> 276s)
        because headings gave up and tables immediately re-attempted the identical
        parse. So the timeout is recorded, and every later pdfminer-dependent
        operator on that page declines rather than repeating it.
        """
        self._pdfminer_timeouts.add(page_no)

    def pdfminer_timed_out(self, page_no: int) -> bool:
        return page_no in self._pdfminer_timeouts

    def page_objects(self, page_no: int) -> dict[str, int]:
        """Count the objects the source page actually contains.

        Evaluation needs this: "expected images" has to come from the file, not
        from the extraction being judged, or the measurement is circular.
        """
        counts = {"images": 0, "paths": 0, "texts": 0}
        page = None
        try:
            with _PDFIUM_LOCK:
                page = self._pdf[page_no - 1]
                for obj in page.get_objects():
                    kind = int(obj.type)
                    if kind == 3:      # FPDF_PAGEOBJ_IMAGE
                        counts["images"] += 1
                    elif kind == 2:    # FPDF_PAGEOBJ_PATH
                        counts["paths"] += 1
                    elif kind == 1:    # FPDF_PAGEOBJ_TEXT
                        counts["texts"] += 1
        except Exception:  # noqa: BLE001 - an inventory is a nicety, never fatal
            pass
        finally:
            if page is not None:
                try:
                    with _PDFIUM_LOCK:
                        page.close()
                except Exception:  # noqa: BLE001
                    pass
        return counts

    def page_profile(self, page_no: int) -> dict[str, Any]:
        """Facts PDFium can report in milliseconds, used to skip expensive work.

        Both expensive operators answer a question PDFium can settle first:

        * `find_tables` with ruling lines cannot find a table on a page with no
          ruling lines. Counting *wide, thin* vector paths is a far sharper test
          than counting paths — page borders, underlines and figure strokes are
          all paths, and 78 % of pages in this corpus have some.
        * typographic headings cannot exist on a page whose largest text is the
          same size as its body text, measured by font size rather than box height.

        Neither check changes what an operator does; they only decide whether it
        can possibly do anything. The heading test is deliberately looser than the
        operator's own threshold (1.08 vs 1.12) so a real heading is never missed.
        """
        cached = self._profiles.get(page_no)
        if cached is not None:
            return cached

        profile: dict[str, Any] = {"n_paths": 1, "n_rules": 999, "has_oversized_text": True, "profiled": False}
        page = None
        textpage = None
        try:
            with _PDFIUM_LOCK:
                page = self._pdf[page_no - 1]
                page_width = float(page.get_width())
                n_paths = 0
                n_rules = 0
                for obj in page.get_objects():
                    if int(obj.type) != 2:  # FPDF_PAGEOBJ_PATH
                        continue
                    n_paths += 1
                    try:
                        left, bottom, right, top = obj.get_pos()
                    except Exception:  # noqa: BLE001
                        continue
                    if page_width and (right - left) > page_width * 0.3 and (top - bottom) < 2.5:
                        n_rules += 1
                profile["n_paths"] = n_paths
                profile["n_rules"] = n_rules

                textpage = page.get_textpage()
                count = textpage.count_chars()
                get_font_size = _font_size_fn()
                if count and get_font_size is not None:
                    # Sampled: the largest text on a page is not a rare glyph.
                    stride = max(1, count // 500)
                    sizes: list[float] = []
                    for index in range(0, count, stride):
                        try:
                            value = float(get_font_size(textpage.raw, index))
                        except Exception:  # noqa: BLE001
                            continue
                        if value > 0.5:
                            sizes.append(value)
                    if sizes:
                        sizes.sort()
                        median = sizes[len(sizes) // 2]
                        largest = sizes[-1]
                        profile["has_oversized_text"] = bool(
                            median > 0 and largest >= median * HEADING_PRE_FILTER_RATIO
                        )
                        profile["median_font"] = round(median, 2)
                        profile["largest_font"] = round(largest, 2)
                profile["profiled"] = True
        except Exception as exc:  # noqa: BLE001 - a profile is an optimisation, never a failure
            # On any doubt, assume the expensive operators might find something.
            profile = {"n_paths": 1, "n_rules": 999, "has_oversized_text": True, "profiled": False, "error": str(exc)}
        finally:
            if textpage is not None:
                try:
                    with _PDFIUM_LOCK:
                        textpage.close()
                except Exception:  # noqa: BLE001
                    pass
            if page is not None:
                try:
                    with _PDFIUM_LOCK:
                        page.close()
                except Exception:  # noqa: BLE001
                    pass
        if len(self._profiles) > 4096:
            self._profiles.clear()
        self._profiles[page_no] = profile
        return profile

    # ── pdfplumber, cached per document ─────────────────────────────────────

    def plumber_page(self, page_no: int) -> Any | None:
        """The pdfplumber Page for this page, from a handle cached per document.

        Profiling put ~100% of PDF extraction time in the two pdfplumber
        operators, and a large part of that was doing the same work twice: each
        opened the document and parsed the page's content stream independently.
        pdfplumber caches the parsed layout objects on the Page it hands out, so
        returning the SAME Page to both operators makes the second one nearly
        free — the expensive `parse_objects()` runs once per page instead of twice.

        Memory is bounded by a sliding window: `flush_cache()` drops a page's
        parsed objects once it falls out of the window, so a 1428-page document
        does not accumulate 1428 parsed pages in a worker.
        """
        pdf = self._plumber
        if pdf is None:
            import pdfplumber

            pdf = self._plumber = pdfplumber.open(str(self.path))
            self._plumber_page_count = len(pdf.pages)
            self._plumber_pdf = pdf
        if page_no < 1 or page_no > self._plumber_page_count:
            return None
        page = pdf.pages[page_no - 1]
        self._plumber_touch(page_no)
        return page

    def _plumber_touch(self, page_no: int) -> None:
        seen = self._plumber_seen
        if page_no in seen:
            seen.remove(page_no)
        seen.append(page_no)
        if len(self._profiles) > 4096:
            # Profiles are small, but a huge document should still be bounded.
            self._profiles.clear()
        while len(seen) > PLUMBER_PAGE_WINDOW:
            stale = seen.pop(0)
            try:
                self._plumber.pages[stale - 1].flush_cache()
            except Exception:  # noqa: BLE001 - eviction is best-effort
                pass

    def _close_plumber(self) -> None:
        pdf = self._plumber
        self._plumber = None
        if pdf is not None:
            try:
                pdf.close()
            except Exception:  # noqa: BLE001
                pass

    # ── deep signals ────────────────────────────────────────────────────────

    def deep_signals(self, page_no: int) -> dict[str, Any]:
        """Tier-2 signals, computed only when the router or the gate asks.

        pdfplumber parses page structure properly, which is far more expensive
        than reading the text layer — so it is never part of enumeration.
        """
        out: dict[str, Any] = {}
        try:
            with_handle = self.plumber_page(page_no)
            if with_handle is not None:
                page = with_handle
                page_area = float(page.width * page.height) or 1.0
                image_area = 0.0
                for image in page.images:
                    image_area += max(0.0, float(image["x1"]) - float(image["x0"])) * max(
                        0.0, float(image["bottom"]) - float(image["top"])
                    )
                out["n_images"] = len(page.images)
                out["image_area_frac"] = round(min(1.0, image_area / page_area), 4)
                out["n_lines"] = len(page.lines) + len(getattr(page, "edges", []))
                out["n_rects"] = len(page.rects)
                font_sizes = sorted({round(float(c["size"]), 1) for c in page.chars})
                out["font_sizes"] = font_sizes[-8:]
                out["n_chars"] = len(page.chars)
                try:
                    tables = page.find_tables()
                    out["n_table_candidates"] = len(tables)
                    out["has_table_candidate"] = bool(tables)
                except Exception:  # noqa: BLE001 - table detection is best-effort by design
                    out["n_table_candidates"] = 0
                    out["has_table_candidate"] = False
        except Exception as exc:  # noqa: BLE001 - signals are advisory; never fatal
            out["deep_signals_error"] = f"{type(exc).__name__}: {exc}"
        return out

    # ── rasterisation ───────────────────────────────────────────────────────

    def render(self, page_no: int, dpi: int) -> Path:
        target = self.raster_path(page_no, dpi)
        if target.exists() and target.stat().st_size > 0:
            return target
        with _PDFIUM_LOCK:
            page = self._pdf[page_no - 1]
        try:
            with _PDFIUM_LOCK:
                bitmap = page.render(scale=dpi / 72.0)
            try:
                image = bitmap.to_pil()
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Write to a temp name then rename: a half-written PNG must
                    # never be seen by another worker.
                    tmp = target.with_suffix(".png.part")
                    image.save(tmp, format="PNG")
                    tmp.replace(target)
                finally:
                    image.close()
            finally:
                bitmap.close()
        except Exception as exc:  # noqa: BLE001
            raise OpError(f"rasterising page {page_no} at {dpi} dpi failed: {exc}") from exc
        finally:
            with _PDFIUM_LOCK:
                page.close()
        self.enforce_raster_cache()
        return target


# ─────────────────────────────────────────────────────────────────────────────
# Line assembly
# ─────────────────────────────────────────────────────────────────────────────

#: Parsed pdfplumber pages kept per worker before the oldest is flushed.
PLUMBER_PAGE_WINDOW = 48

#: `FPDFText_GetFontSize` is not wrapped by pypdfium2's helper API, but it is
#: exported by the bundled library and reachable through the raw module. Bound
#: once; if a future version renames it, line extraction silently loses font
#: sizes and headings fall back to the box-height heuristic.
_FONT_SIZE_FN: Any = None
_FONT_SIZE_BOUND = False


def _font_size_fn() -> Any:
    global _FONT_SIZE_FN, _FONT_SIZE_BOUND
    if not _FONT_SIZE_BOUND:
        _FONT_SIZE_BOUND = True
        try:
            import pypdfium2.raw as _raw

            _FONT_SIZE_FN = getattr(_raw, "FPDFText_GetFontSize", None)
        except Exception:  # noqa: BLE001
            _FONT_SIZE_FN = None
    return _FONT_SIZE_FN


#: A page whose largest text is not this much taller than its body text cannot
#: contain a typographic heading. Deliberately looser than the operator's own
#: 1.12 threshold, so the pre-filter never suppresses a real heading.
HEADING_PRE_FILTER_RATIO = 1.08

#: A gap wider than this fraction of the average character width is a space.
SPACE_GAP_FACTOR = 0.28

#: Two characters belong to the same line when their vertical centres are within
#: this fraction of the line height.
LINE_TOLERANCE = 0.55


def group_chars_into_lines(chars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group positioned characters into lines, resolving both axes.

    Two separate problems, solved in that order:

    1. **Which line?** Characters are binned by vertical centre and adjacent bins
       merged. A proximity threshold is brittle exactly where it matters — a line
       mixing font sizes (a superscript, an italic run, a drop cap) can exceed the
       threshold, split, and interleave into output like
       ``e u e e a a e e s a ue cos ec``.
    2. **Which column?** A single y-band can contain the tail of the left column
       and the head of the right one, plus a table caption sitting alongside body
       text. Sorting that band by x interleaves them. So a band is split into runs
       at horizontal gaps far wider than the spacing inside a phrase, and each run
       becomes its own line.

    Reading order is descending y: PDF y grows upward, so the first line of the
    page has the largest centre.
    """
    if not chars:
        return []

    # Band height comes from BOX height, never from font size. Font size (~9pt on
    # a body page) is far larger than the vertical spread of a line's characters,
    # so binning by it merges adjacent lines into one band and interleaves their
    # words — which is exactly what an earlier version of this function did,
    # producing `Niv DayanP,l Mioposshe Twitto` and phantom 140pt "headings".
    # Font size is the right metric for deciding what IS a heading, and the wrong
    # one for deciding where a line ENDS.
    heights = sorted(height for height in (c["bottom"] - c["top"] for c in chars) if height > 0.5)
    median_height = heights[len(heights) // 2] if heights else 10.0
    bin_width = max(0.4, median_height / 2.0)

    centres = [(c["top"] + c["bottom"]) / 2.0 for c in chars]
    lowest = min(centres)
    buckets: dict[int, list[dict[str, Any]]] = {}
    for char, centre in zip(chars, centres):
        buckets.setdefault(int((centre - lowest) / bin_width), []).append(char)

    keys = sorted(buckets)
    bands: list[list[int]] = [[keys[0]]]
    for key in keys[1:]:
        # One empty bin is intra-line noise (an accent, a subscript); two or more
        # is the leading between lines.
        if key - bands[-1][-1] <= 1:
            bands[-1].append(key)
        else:
            bands.append([key])

    lines: list[dict[str, Any]] = []
    for band in bands:
        band_chars = [char for key in band for char in buckets[key]]
        for run in _split_runs(band_chars):
            line = _assemble_line(run)
            if line["text"]:
                lines.append(line)
    lines.sort(key=lambda line: -((line["top"] + line["bottom"]) / 2.0))
    return lines


def _split_runs(chars: list[dict[str, Any]], factor: float = 2.5) -> list[list[dict[str, Any]]]:
    """Split one y-band into horizontal runs separated by real gaps.

    The threshold is a multiple of the *median character width* on that band, so
    it adapts to the font. Word spaces are a fraction of a character width and a
    column gutter is several, which is a wide enough margin to separate them
    without measuring the layout.
    """
    ordered = sorted(chars, key=lambda c: c["x0"])
    if len(ordered) < 2:
        return [ordered]
    widths = sorted(max(0.01, c["x1"] - c["x0"]) for c in ordered)
    median_width = widths[len(widths) // 2]
    threshold = max(2.0, median_width * factor)

    runs: list[list[dict[str, Any]]] = [[ordered[0]]]
    for previous, char in zip(ordered, ordered[1:]):
        if char["x0"] - previous["x1"] > threshold:
            runs.append([char])
        else:
            runs[-1].append(char)
    return runs


def _assemble_line(chars: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble one run into a line, reconstructing the spaces it omits.

    pdfplumber exposes words rather than whitespace and PDFium reports positions
    rather than every space glyph, so joining characters naively produced headings
    like ``3PROMISE:LSM-TREE&CUCKOOFILTER``.
    """
    ordered = sorted(chars, key=lambda c: c["x0"])
    sizes = [float(c["size"]) for c in ordered if c.get("size")]
    typical_size = sorted(sizes)[len(sizes) // 2] if sizes else 10.0
    widths = sorted(max(0.01, c["x1"] - c["x0"]) for c in ordered)
    typical_width = widths[len(widths) // 2]
    # A word space is a fraction of a character width; measured on this corpus it
    # is 4.3pt at a 17.2pt font, with intra-word gaps near zero.
    gap_threshold = max(0.6, min(typical_size * 0.2, typical_width * 1.2))

    pieces: list[str] = []
    previous_x1: float | None = None
    for char in ordered:
        if previous_x1 is not None and char["x0"] - previous_x1 > gap_threshold:
            pieces.append(" ")
        pieces.append(str(char["char"]))
        previous_x1 = char["x1"]

    return {
        "text": "".join(pieces).strip(),
        "x0": min(c["x0"] for c in ordered),
        "x1": max(c["x1"] for c in ordered),
        "top": min(c["top"] for c in ordered),
        "bottom": max(c["bottom"] for c in ordered),
        "size": max(0.1, typical_size),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Format plugin surface
# ─────────────────────────────────────────────────────────────────────────────


def match(head: bytes, path: str) -> float:
    if head.startswith(b"%PDF-"):
        return 1.0
    if path.lower().endswith(PDF_EXTENSIONS):
        return 0.7
    return 0.0


def open_session(path: Path, doc_id: str, config: Any) -> PdfSession:
    return PdfSession(path=path, doc_id=doc_id, config=config, work_dir=Path(config.work_dir))


def enumerate_units(ctx: FormatContext) -> Iterator[UnitSeed]:
    session = PdfSession(
        path=ctx.path,
        doc_id=ctx.doc_id,
        config=ctx.scratch.get("config"),
        work_dir=Path(getattr(ctx.scratch.get("config"), "work_dir", ctx.work_dir)),
    )
    try:
        yield from session.enumerate_units()
    finally:
        session.close()


def detect_encryption(path: Path) -> dict[str, Any]:
    """Report whether a PDF is encrypted, without needing pdfinfo.

    An encrypted PDF is a legitimate input we cannot read; the caller marks the
    document and moves on rather than crashing the run.
    """
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001
        return {"openable": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        return {"openable": True, "pages": len(pdf)}
    finally:
        pdf.close()

# ─────────────────────────────────────────────────────────────────────────────
# Line assembly
# ─────────────────────────────────────────────────────────────────────────────

#: Parsed pdfplumber pages kept per worker before the oldest is flushed.
PLUMBER_PAGE_WINDOW = 48

#: `FPDFText_GetFontSize` is not wrapped by pypdfium2's helper API, but it is
#: exported by the bundled library and reachable through the raw module. Bound
#: once; if a future version renames it, line extraction silently loses font
#: sizes and headings fall back to the box-height heuristic.
_FONT_SIZE_FN: Any = None
_FONT_SIZE_BOUND = False


def _font_size_fn() -> Any:
    global _FONT_SIZE_FN, _FONT_SIZE_BOUND
    if not _FONT_SIZE_BOUND:
        _FONT_SIZE_BOUND = True
        try:
            import pypdfium2.raw as _raw

            _FONT_SIZE_FN = getattr(_raw, "FPDFText_GetFontSize", None)
        except Exception:  # noqa: BLE001
            _FONT_SIZE_FN = None
    return _FONT_SIZE_FN


#: A page whose largest text is not this much taller than its body text cannot
#: contain a typographic heading. Deliberately looser than the operator's own
#: 1.12 threshold, so the pre-filter never suppresses a real heading.
HEADING_PRE_FILTER_RATIO = 1.08

#: A gap wider than this fraction of the average character width is a space.
SPACE_GAP_FACTOR = 0.28

#: Two characters belong to the same line when their vertical centres are within
