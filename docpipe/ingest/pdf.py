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

    def enumerate_units(self) -> Iterator[UnitSeed]:
        """Stream page seeds, filling the text layer as we go.

        Nothing is rasterised here: the router decides from text, geometry and
        cheap structure whether a page deserves pixels at all.
        """
        for page_no in range(1, self._units + 1):
            try:
                width, height = self.page_size(page_no)
                text = self.page_text(page_no)
            except OpError as exc:
                yield UnitSeed(
                    page_no=page_no, kind="pdf_page", signals=PageSignals(), payload={"error": str(exc)}
                )
                continue
            signals = page_signals(text, width, height)
            yield UnitSeed(page_no=page_no, kind="pdf_page", signals=signals, text_layer=text)

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
