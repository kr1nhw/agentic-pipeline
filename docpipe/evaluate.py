"""Extraction evaluation: turn "is it correct?" into a number.

The gate answers *"did extraction fail?"*. This answers a different and harder
question: **"did extraction lose anything?"** They are not the same, and the
difference is not academic — a slide deck whose text is perfect but whose eleven
images became eleven empty `figure` blocks passes every gate and is still an
incomplete extraction.

So evaluation starts from the **source**, not from the output. For every unit the
source is re-read to establish what was there (characters, images, tables, ruling
lines), and every one of those objects must be accounted for in the output:

    extracted   present in the blocks
    described   present as a figure/caption the vision path read
    linked      referenced by URI but not read
    missing     not accounted for at all   <- the number that matters

Everything here is deterministic and free. The vision model is available as one
optional signal (`--judge-sample`), used on a sample of pages to catch what
metrics cannot express: content that was transcribed *wrongly* rather than
*misplaced* — a metric can see a missing table, never a hallucinated cell.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from concurrent import futures
from pathlib import Path
from typing import Any, Iterable, Sequence

from docpipe.config import Config
from docpipe.textstats import WORD_SHAPE_FLOOR, text_word_shape
from docpipe.schema import Block, PageRecord

#: Words for the recall comparison. CJK is matched per character, because a
#: Chinese document has no spaces to split on and a "word" would be the line.
WORD = re.compile(r"[A-Za-z0-9_]{2,}")
CJK = re.compile(r"[\u4e00-\u9fff]")

# The garbling threshold is `textstats.WORD_SHAPE_FLOOR`, shared with the reader.
# A local limit lived here and it measured something else entirely: the share of
# alphanumeric characters, whitespace included, which is a measure of how much
# layout the extractor preserved rather than of whether the text is text.

#: Weights for the document score. Text dominates because it is what most
#: consumers actually use; images and tables are where the silent losses live.
WEIGHTS = {"text_recall": 0.5, "image_accounting": 0.2, "table_survival": 0.2, "clean_pages": 0.1}


@dataclass
class PageVerdict:
    page_id: str
    kind: str
    source_chars: int = 0
    output_chars: int = 0
    text_recall: float = 1.0
    source_images: int = 0
    accounted_images: int = 0
    source_tables: int = 0
    output_tables: int = 0
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    linked_images: int = 0
    #: Tokens missing from this unit that the document keeps elsewhere: repetition
    #: removed on purpose, not content lost.
    deduplicated_tokens: int = 0
    judge: dict[str, Any] | None = None

    @property
    def image_recall(self) -> float:
        if self.source_images == 0:
            return 1.0
        return self.accounted_images / self.source_images

    @property
    def table_recall(self) -> float:
        if self.source_tables == 0:
            return 1.0
        return min(1.0, self.output_tables / self.source_tables)

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "kind": self.kind,
            "source_chars": self.source_chars,
            "output_chars": self.output_chars,
            "text_recall": round(self.text_recall, 3),
            "source_images": self.source_images,
            "accounted_images": self.accounted_images,
            "linked_images": self.linked_images,
            "source_tables": self.source_tables,
            "output_tables": self.output_tables,
            "flags": self.flags,
            "notes": self.notes,
            "judge": self.judge,
        }


def tokens(text: str) -> list[str]:
    """Comparison units: words for Latin script, characters for CJK."""
    return [match.group(0).lower() for match in WORD.finditer(text)] + CJK.findall(text)


def text_recall(source: str, output: str) -> float:
    """Fraction of the source's text that the output actually contains.

    A multiset comparison, not a diff: the output legitimately reorders and
    re-wraps text, and what matters is whether the content survived, not whether
    the whitespace agrees.
    """
    source_tokens = tokens(source)
    if not source_tokens:
        return 1.0
    from collections import Counter

    available = Counter(source_tokens)
    produced = Counter(tokens(output))
    matched = sum(min(count, produced.get(token, 0)) for token, count in available.items())
    return matched / len(source_tokens)


def recall_split(source: str, output: str, document_output: str = "") -> tuple[float, int]:
    """Recall, plus how much of the shortfall is repetition rather than absence.

    A multiset comparison counts every removed duplicate as loss, which is right
    for measuring a page and wrong for judging it. The pipeline removes a line that
    repeats on every page — an RFC's header, a journal's running title — keeping the
    first occurrence, and that is a deliberate transformation rather than a defect:
    the information is still in the document.

    Measured on the harvested corpus, of 571 tokens missing from the 39 pages the
    old rule flagged, **560 (98.1 %) were present elsewhere in the same document**
    and 11 numeric literals were gone. Reporting all 39 as `text_lost` put a
    deliberate transformation and a real defect in the same column.

    Returns (recall, tokens present elsewhere in the document). Recall counts a
    deduplicated token as kept, because a reader of the document still has it.
    """
    source_tokens = tokens(source)
    if not source_tokens:
        return 1.0, 0
    from collections import Counter

    available = Counter(source_tokens)
    produced = Counter(tokens(output))
    elsewhere = Counter(tokens(document_output)) if document_output else Counter()
    deduplicated = 0
    for token, count in available.items():
        shortfall = count - min(count, produced.get(token, 0))
        if shortfall > 0 and elsewhere:
            deduplicated += min(shortfall, elsewhere.get(token, 0))
    matched = sum(min(count, produced.get(token, 0)) for token, count in available.items())
    return (matched + deduplicated) / len(source_tokens), deduplicated


def looks_garbled(text: str) -> bool:
    """Is this output text, or the debris of a text layer that was guessed at?

    The measure is word shape: the share of letter-bearing tokens that are letters
    only. It is the SAME measure the reader uses to decide whether a text layer can
    be trusted, imported so the two cannot drift apart — and they had drifted.

    This function used to count the share of alphanumeric characters over all
    characters, which counts newlines and the indentation a layout-preserving
    extractor emits. A chart page is then mostly whitespace by character count:

        '80\n              100\n              75\n'   ->  0.27  "garbled"

    On the harvested corpus it flagged 110 pages, and the samples were charts,
    result tables, a roll-call list of names and a letterspaced masthead — every
    character meaningful. Meanwhile the one genuinely mis-decoded document to hand
    (a 1950s scan whose layer renders `t(R)` as `~'(R)`) scored 0.90 on it.

    Word shape, measured on the same documents:

        topology paper, mis-decoded   0.41   -> flagged
        2021 ACL paper                0.92   -> not flagged
        Congressional Record          0.95   -> not flagged
        IRS form 1040                 0.86   -> not flagged

    Only tokens containing a letter vote, so a page of axis labels or a results
    table is judged on its words rather than on its numbers.
    """
    stripped = text.strip()
    if len(stripped) < 40:
        return False
    if stripped.count("\ufffd") > len(stripped) * 0.02:
        # Replacement characters are a decoding failure with no ambiguity.
        return True
    return text_word_shape(stripped, letters_only=True) < WORD_SHAPE_FLOOR


def source_facts(session: Any, page_no: int, kind: str) -> dict[str, int]:
    """What the SOURCE says is on this unit.

    Read from the format's own structures — PDFium page objects for a PDF,
    slide shapes for a deck — so "expected" is evidence rather than an assumption
    derived from the extraction being evaluated.
    """
    facts = {"images": 0, "tables": 0, "chars": 0}
    try:
        if hasattr(session, "page_objects"):
            facts.update(session.page_objects(page_no))
        if kind == "slide" and hasattr(session, "slide_shapes"):
            shapes = session.slide_shapes(page_no)
            facts["images"] = sum(1 for shape in shapes if shape.get("is_picture"))
            facts["tables"] = sum(1 for shape in shapes if shape.get("has_table"))
        elif kind == "pdf_page" and hasattr(session, "page_profile"):
            profile = session.page_profile(page_no)
            facts["images"] = int(profile.get("n_images", 0))
    except Exception:  # noqa: BLE001 - expectations are best-effort, never fatal
        pass
    return facts


def table_check_skipped(session: Any, page_no: int) -> str:
    """Would the extraction operator decline to look for tables on this page?

    Returns the reason, or an empty string. The evaluator must apply the same
    guards the operator applies, for two reasons and neither is cosmetic:

    * **The measurement.** Counting a table the pipeline deliberately did not look
      for, on a page it declined by design, reports a defect that is really a cost
      decision — and hides the decision, which is the thing worth reviewing.
    * **The wall clock.** Table detection is the expensive part of evaluation:
      measured on 98 pages, 43 s of 52 s. The pages the operator declines are the
      ones with tens of thousands of vector objects, i.e. the slowest pages to
      detect on. An evaluation of a 3 200-page corpus ran for over ninety minutes
      doing detection on figure-heavy pages to establish expectations for a search
      that never happens.
    """
    try:
        if hasattr(session, "pdfminer_timed_out") and session.pdfminer_timed_out(page_no):
            return "pdfminer already exceeded its budget on this page"
        if hasattr(session, "page_profile"):
            profile = session.page_profile(page_no)
            if profile.get("profiled") and profile.get("n_paths", 1) == 0:
                return "the page has no vector paths"
        return ""
    except Exception:  # noqa: BLE001 - an unavailable guard is not a reason to skip
        return ""


def expected_tables(session: Any, page_no: int, kind: str) -> int:
    """Tables the source suggests, on the same terms the run extracts them.

    Detection alone is not the expectation. pdfplumber's line strategy returns
    every rule-bounded region, including single-row boxes and empty frames, and
    the operator rejects those on purpose — so an evaluator that counts raw
    detections reports content loss for tables the pipeline was right not to emit.
    Measured on the harvested corpus: 68 pages flagged `table_missing`, of which
    five carried a table block at all. It now applies the operator's own
    acceptance rule, imported rather than restated, so the two cannot drift.
    """
    if kind != "pdf_page" or not hasattr(session, "plumber_page"):
        return 0
    from docpipe.ops.struct_ops import VECTOR_OBJECT_LIMIT, table_rows_accepted

    if table_check_skipped(session, page_no):
        return 0
    try:
        page = session.plumber_page(page_no)
        if page is None:
            return 0
        # The operator declines pages whose vector count says "figure, not table
        # grid", because pdfplumber walks every object. Detecting anyway costs the
        # same walk for a search the run would never perform.
        vectors = len(page.lines) + len(page.rects) + len(getattr(page, "curves", []))
        if VECTOR_OBJECT_LIMIT > 0 and vectors > VECTOR_OBJECT_LIMIT:
            return 0
        found = page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"})
        count = 0
        for table in found:
            extracted = table.extract() or []
            rows = [
                ["" if cell is None else str(cell).strip() for cell in (row or [])]
                for row in extracted
                if row is not None
            ]
            accepted, _ = table_rows_accepted(rows)
            if accepted:
                count += 1
        return count
    except Exception:  # noqa: BLE001
        return 0


def evaluate_page(
    record: PageRecord,
    session: Any,
    *,
    blocks: Sequence[Block],
    check_tables: bool = True,
    document_output: str = "",
) -> PageVerdict:
    """Judge one unit: what was there, what came out, what is unaccounted for."""
    verdict = PageVerdict(page_id=record.page_id, kind=record.kind)
    verdict.source_chars = record.text_layer_chars or len((record.text_layer or "").strip())
    output_text = "\n".join(block.text for block in blocks if block.text)
    verdict.output_chars = len(output_text.strip())

    if record.text_layer:
        verdict.text_recall, verdict.deduplicated_tokens = recall_split(
            record.text_layer, output_text, document_output
        )
        if verdict.text_recall < 0.98:
            verdict.flags.append(
                f"text_lost ({verdict.text_recall:.0%} of source tokens present"
                + (f", {verdict.deduplicated_tokens} kept elsewhere in the document" if verdict.deduplicated_tokens else "")
                + ")"
            )

    if verdict.source_chars and not verdict.output_chars:
        verdict.flags.append("empty_output (source has text, output has none)")
    if output_text and looks_garbled(output_text):
        verdict.flags.append("garbled (output text is mostly non-alphanumeric)")

    facts = source_facts(session, record.page_no, record.kind)
    verdict.source_images = facts.get("images", 0)
    # An image counts as accounted for only if something was EXTRACTED from it —
    # a description, a caption, transcribed content. A figure block that merely
    # points at a file is `linked`, not read.
    #
    # The first version of this accepted a URI as proof and reported 100 % image
    # accounting for a deck whose slides had produced no content at all. The agent
    # judge caught it, reporting "提取结果为空" — the extraction is empty — on the
    # same pages the metric called complete. A metric that counts a pointer as
    # content is worse than no metric, because it reports success.
    verdict.accounted_images = sum(
        1 for block in blocks if block.type in ("figure", "caption") and block.text.strip()
    )
    verdict.linked_images = sum(
        1
        for block in blocks
        if block.type == "figure" and not block.text.strip() and block.image and block.image.uri
    )
    if verdict.linked_images and verdict.source_images:
        verdict.notes.append(
            f"{verdict.linked_images} image(s) linked but not read; "
            "escalation or an explicit describe step would extract them"
        )
    if verdict.source_images and verdict.accounted_images < verdict.source_images:
        verdict.flags.append(
            f"image_unaccounted ({verdict.accounted_images}/{verdict.source_images} accounted for)"
        )

    if check_tables:
        verdict.source_tables = expected_tables(session, record.page_no, record.kind)
        if verdict.source_tables == 0 and record.kind == "pdf_page":
            # Say when no expectation could be established, rather than letting a
            # skipped check read as a page with no tables.
            reason = table_check_skipped(session, record.page_no)
            if reason:
                verdict.notes.append(f"table expectation not established: {reason}")
    verdict.output_tables = sum(1 for block in blocks if block.type == "table")
    if verdict.source_tables and verdict.output_tables < verdict.source_tables:
        verdict.flags.append(
            f"table_missing ({verdict.output_tables}/{verdict.source_tables} extracted)"
        )
    return verdict


def _mean(values: Iterable[float | None]) -> float | None:
    """The mean of the values that exist, or None when none do."""
    present = [value for value in values if value is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def score(verdicts: Sequence[PageVerdict]) -> dict[str, Any]:
    """Aggregate page verdicts into the document's numbers."""
    if not verdicts:
        return {"pages": 0, "score": 0.0}

    text = sum(v.text_recall for v in verdicts) / len(verdicts)
    images = sum(v.image_recall for v in verdicts) / len(verdicts)
    tables = sum(v.table_recall for v in verdicts) / len(verdicts)
    clean = sum(1 for v in verdicts if not v.flags) / len(verdicts)
    total = (
        WEIGHTS["text_recall"] * text
        + WEIGHTS["image_accounting"] * images
        + WEIGHTS["table_survival"] * tables
        + WEIGHTS["clean_pages"] * clean
    )
    return {
        "pages": len(verdicts),
        "pages_with_flags": sum(1 for v in verdicts if v.flags),
        "text_recall": round(text, 4),
        "image_accounting": round(images, 4),
        "table_survival": round(tables, 4),
        "clean_pages": round(clean, 4),
        "score": round(total, 4),
        "flag_counts": _flag_counts(verdicts),
    }


def _flag_counts(verdicts: Sequence[PageVerdict]) -> dict[str, int]:
    """Count by flag CATEGORY, taken from the stable prefix before the first
    space. Parsing the prose to recover a key produced a bucket literally named
    "1" the first time this ran."""
    counts: dict[str, int] = {}
    for verdict in verdicts:
        for flag in verdict.flags:
            key = flag.split(" ", 1)[0]
            counts[key] = counts.get(key, 0) + 1
    return counts


def load_run(out_dir: Path) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Read a finished run's artifacts back."""
    import pyarrow.parquet as pq

    pages_path = out_dir / "pages.parquet"
    blocks_path = out_dir / "blocks.parquet"
    if not pages_path.exists() or not blocks_path.exists():
        raise FileNotFoundError(f"{out_dir} has no pages.parquet/blocks.parquet to evaluate")

    pages = pq.read_table(pages_path).to_pylist()
    by_page: dict[str, list[dict[str, Any]]] = {}
    for row in pq.read_table(blocks_path).to_pylist():
        by_page.setdefault(row["page_id"], []).append(row)
    return pages, by_page


JUDGE_SYSTEM = (
    "You audit document extraction. You are shown a page image and the content that was "
    "extracted from it. You report what is MISSING or WRONG, never what you would have "
    "written instead. If the extraction is faithful, say so plainly."
)

JUDGE_PROMPT = """Below is the content extracted from the page image above.

Judge whether it faithfully represents the page. Return JSON only:
{{"verdict":"complete|partial|wrong","missing":["..."],"wrong":["..."],
"wrong_order":true|false,"notes":"one sentence"}}

Rules:
- "complete" means a reader would get everything the page conveys. Images, charts,
  tables and formulas count as content: text that merely mentions them does not
  replace them.
- List an item under "missing" only if you can see it on the page and it is absent
  from the extraction. Do not list things you cannot see.
- "wrong" is for content that is present but incorrect: stray characters, merged
  columns, a number that does not match the page.

--- extracted content ---
{extracted}
--- end ---"""


def agent_judge(
    session: Any,
    record: PageRecord,
    blocks: Sequence[Block],
    *,
    vl: Any,
    config: Config,
    dpi: int | None = None,
) -> dict[str, Any]:
    """Ask the vision model whether the extraction is faithful.

    Metrics catch what is *missing* from a known inventory; only a model can catch
    what is *wrong* — a transcribed number that disagrees with the page, or two
    columns merged into one paragraph. This is the complement, not a replacement.

    The image is mandatory. Judging transcription quality from text alone is how
    you get a model agreeing with the thing it is supposed to be checking.
    """
    if vl is None:
        return {"skipped": "no model available"}
    extracted = "\n".join(
        f"[{block.type}] {block.text.strip()[:400]}" for block in blocks if block.text.strip()
    )[:6000]
    raster = None
    try:
        if session is not None and hasattr(session, "render"):
            raster = session.render(
                record.page_no,
                dpi or int(getattr(config, "min_extraction_dpi", 110)),
            )
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"cannot render the page for judging: {exc}"}
    if raster is None:
        return {"skipped": "no page image available to judge against"}

    try:
        response = vl.complete(
            system=JUDGE_SYSTEM,
            user=JUDGE_PROMPT.format(extracted=extracted or "(nothing was extracted)"),
            images=[raster],
            thinking=False,
            max_tokens=900,
            json_object=True,
            purpose="evaluate",
        )
    except Exception as exc:  # noqa: BLE001 - a failed judge is a missing signal, not a failed run
        return {"error": f"{type(exc).__name__}: {exc}"}

    from docpipe.ops.media_ops import _parse_json

    payload = _parse_json(response.text) or {}
    return {
        "verdict": payload.get("verdict", "unknown"),
        "missing": payload.get("missing") or [],
        "wrong": payload.get("wrong") or [],
        "wrong_order": bool(payload.get("wrong_order")),
        "notes": payload.get("notes", ""),
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
    }


def evaluate_run(
    out_dir: Path,
    registry: Any,
    config: Config,
    *,
    check_tables: bool = True,
    vl: Any = None,
    judge_sample: int = 0,
) -> dict[str, Any]:
    """Evaluate every document in a finished run.

    Reads the run's own artifacts rather than re-running it: evaluation must
    describe what was produced, not what the pipeline would produce if asked
    again.
    """
    pages, blocks_by_page = load_run(out_dir)
    manifest_path = out_dir / "manifest.json"
    manifest: list[dict[str, Any]] = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_doc = {entry["doc_id"]: entry for entry in manifest}

    grouped = list(_group_by(pages, "doc_id").items())

    def evaluate_document(item: tuple[str, list[dict[str, Any]]]) -> tuple[dict[str, Any], list[PageVerdict]]:
        return _evaluate_document(
            item,
            by_doc=by_doc,
            blocks_by_page=blocks_by_page,
            registry=registry,
            config=config,
            check_tables=check_tables,
            vl=vl,
            judge_sample=judge_sample,
        )

    # Documents are independent: each opens its own session, and nothing is shared
    # but the artifacts being read. Table detection is the expensive part — 43 s of
    # a 52 s evaluation over 98 pages — and it is pure Python walking vector
    # objects, so this scales with cores. A 3 200-page corpus evaluated in one
    # thread ran for over ninety minutes, which is the loop this measurement exists
    # to shorten. The judge path stays sequential: it spends money, and its budget
    # is shared.
    workers = max(1, int(getattr(config, "effective_workers", 1) or 1))
    results: list[tuple[dict[str, Any], list[PageVerdict]]] = []
    if workers > 1 and vl is None and len(grouped) > 1:
        with futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evaluate") as pool:
            results = list(pool.map(evaluate_document, grouped))
    else:
        results = [evaluate_document(item) for item in grouped]

    documents: list[dict[str, Any]] = [document for document, _ in results]
    all_verdicts: list[PageVerdict] = [verdict for _, verdicts in results for verdict in verdicts]

    total = score(all_verdicts) if all_verdicts else {"score": 0.0}
    return {
        "evaluated_at": _now(),
        "out_dir": str(out_dir),
        "documents": documents,
        "totals": {
            "documents": len(documents),
            "pages": total.get("pages", 0),
            "pages_with_flags": total.get("pages_with_flags", 0),
            "text_recall": total.get("text_recall"),
            # Page-level recall is a diagnostic; this is the fidelity a consumer
            # of the document experiences.
            "document_recall": _mean(doc.get("document_recall") for doc in documents),
            "image_accounting": total.get("image_accounting"),
            "table_survival": total.get("table_survival"),
            "score": total.get("score"),
            "flag_counts": total.get("flag_counts", {}),
        },
    }


def _evaluate_document(
    item: tuple[str, list[dict[str, Any]]],
    *,
    by_doc: dict[str, dict[str, Any]],
    blocks_by_page: dict[str, list[dict[str, Any]]],
    registry: Any,
    config: Config,
    check_tables: bool,
    vl: Any,
    judge_sample: int,
) -> tuple[dict[str, Any], list[PageVerdict]]:
    """Evaluate one document. Independent of every other document by construction."""
    doc_id, rows = item
    entry = by_doc.get(doc_id, {})
    source = Path(entry.get("source_path", ""))
    if not source.is_file():
        return (
            {
                "doc_id": doc_id,
                "source_path": str(source),
                "error": "source file is gone; cannot establish expectations",
            },
            [],
        )

    session = _open_session(source, doc_id, registry, config)
    verdicts: list[PageVerdict] = []
    # The whole document's output, so a page can tell "this line was removed
    # because it repeats on every page" from "this line was lost". Every unit's
    # assembled text is on its row, so this needs no extra work.
    document_output = "\n".join((row.get("text") or "") for row in rows)
    try:
        ordered = sorted(rows, key=lambda item: item["page_no"])
        for row in ordered:
            record = _record_from_row(row)
            blocks = [block_from_row(item) for item in blocks_by_page.get(record.page_id, [])]
            verdicts.append(
                evaluate_page(
                    record,
                    session,
                    blocks=blocks,
                    check_tables=check_tables,
                    document_output=document_output,
                )
            )
        if vl is not None and judge_sample > 0:
            _judge_sample(verdicts, ordered, blocks_by_page, session, vl, config, judge_sample)
    finally:
        if session is not None:
            session.close()

    # Document-level recall, as distinct from the per-page average: a document
    # is read as a whole, and its whole is what a consumer receives.
    document_recall, _ = recall_split(
        "\n".join(row.get("text_layer") or "" for row in rows), document_output
    )

    document = {
        "doc_id": doc_id,
        "source_path": str(source),
        "format": entry.get("format"),
        "pipeline_id": rows[0].get("pipeline_id"),
        "document_recall": round(document_recall, 4),
        **score(verdicts),
        "worst_pages": [
            v.as_dict() for v in sorted(verdicts, key=lambda v: (v.text_recall, len(v.flags)))[:5] if v.flags
        ],
        # Every verdict, including the clean ones: a page the agent judged as
        # "partial" while the metrics found nothing is the single most
        # interesting row in this file, and it was being discarded.
        "pages": [v.as_dict() for v in verdicts],
    }
    judged = [v for v in verdicts if v.judge]
    if judged:
        # Report skips and errors explicitly. A judge that silently returns
        # nothing still counts as "judged", and a summary that only tallies
        # verdicts then shows four zeros with no way to tell a model that
        # declined from a model that was never called.
        document["agent_judgement"] = {
            "judged_pages": len(judged),
            "verdicts": {
                verdict: sum(1 for v in judged if (v.judge or {}).get("verdict") == verdict)
                for verdict in ("complete", "partial", "wrong", "unknown")
            },
            "skipped": [
                (v.judge or {}).get("skipped") or (v.judge or {}).get("error")
                for v in judged
                if (v.judge or {}).get("skipped") or (v.judge or {}).get("error")
            ][:5],
            "pages_flagged_by_agent": [
                v.page_id for v in judged if (v.judge or {}).get("verdict") in ("partial", "wrong")
            ],
        }
    return document, verdicts


def _judge_sample(
    verdicts: list[PageVerdict],
    rows: list[dict[str, Any]],
    blocks_by_page: dict[str, list[dict[str, Any]]],
    session: Any,
    vl: Any,
    config: Config,
    sample: int,
) -> None:
    """Judge a sample of pages, preferring the ones that already look wrong.

    Sampling at random would spend model calls confirming pages that are obviously
    fine. Pages a metric already flagged are where a second opinion is worth
    buying; the rest of the sample goes to unflagged pages, because the point of a
    judge is to find what the metrics did NOT.
    """
    flagged = [v for v in verdicts if v.flags]
    clean = [v for v in verdicts if not v.flags]
    chosen = flagged[:sample]
    if len(chosen) < sample:
        step = max(1, len(clean) // max(1, sample - len(chosen)))
        chosen.extend(clean[::step][: sample - len(chosen)])

    by_id = {v.page_id: v for v in verdicts}
    for page_id in [v.page_id for v in chosen]:
        row = next((item for item in rows if item["page_id"] == page_id), None)
        if row is None:
            continue
        record = _record_from_row(row)
        blocks = [block_from_row(item) for item in blocks_by_page.get(page_id, [])]
        by_id[page_id].judge = agent_judge(session, record, blocks, vl=vl, config=config)


def _group_by(rows: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def block_from_row(row: dict[str, Any]) -> Block:
    """Rebuild a Block from its flat parquet row.

    `table_json` and `checks` are stored as JSON text, and the bbox is exploded
    into four columns. Reconstructing properly matters: a judge that sees empty
    `checks` or a `None` table cannot tell an unextracted table from an extracted
    one, and would quietly pass the thing it is meant to catch.
    """
    from docpipe.schema import BBox, ImagePayload, TablePayload

    def _load(value: Any) -> Any:
        if not value:
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    table = _load(row.get("table_json"))
    bbox = None
    if row.get("bbox_x0") is not None:
        bbox = BBox(
            x0=float(row["bbox_x0"]),
            y0=float(row.get("bbox_y0") or 0.0),
            x1=float(row.get("bbox_x1") or 0.0),
            y1=float(row.get("bbox_y1") or 0.0),
        )
    return Block(
        block_id=row["block_id"],
        page_id=row["page_id"],
        order=int(row.get("order") or 0),
        type=row["type"],
        text=row.get("text") or "",
        level=row.get("level"),
        table=TablePayload(**table) if isinstance(table, dict) else None,
        image=ImagePayload(uri=row["image_uri"]) if row.get("image_uri") else None,
        bbox=bbox,
        source_op=row.get("source_op") or "unknown",
        confidence=float(row.get("confidence") or 1.0),
        checks=_load(row.get("checks")) or {},
    )


def _record_from_row(row: dict[str, Any]) -> PageRecord:
    """Rebuild the parts of a page record evaluation needs."""
    from docpipe.schema import PageSignals

    return PageRecord(
        doc_id=row["doc_id"],
        page_no=row["page_no"],
        page_id=row["page_id"],
        kind=row["kind"],
        signals=PageSignals(**(json.loads(row["signals"]) if row.get("signals") else {})),
        text_layer=row.get("text_layer"),
        text_layer_chars=row.get("text_layer_chars") or 0,
    )


def _open_session(path: Path, doc_id: str, registry: Any, config: Config) -> Any:
    """Open the document the way the run would, so expectations come from the same
    readers that produced the output."""
    from docpipe.ingest.base import sniff

    try:
        matches = registry.formats_matching(sniff(path), str(path))
    except Exception:  # noqa: BLE001
        return None
    if not matches:
        return None
    _, spec = matches[0]
    if spec.session_factory is None:
        return None
    try:
        return spec.session_factory(path, doc_id, config)
    except Exception:  # noqa: BLE001
        return None


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def write_evaluation(out_dir: Path, evaluation: dict[str, Any]) -> Path:
    target = out_dir / "evaluation.json"
    target.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return target


def _percent(value: Any) -> str:
    """Format a ratio that may not exist.

    A recovered run, or one whose every document failed to open, has nothing to
    average: the aggregate is `None`, and `f"{None:.1%}"` raises inside the
    summary — turning "there was nothing to measure" into a traceback at the end
    of an otherwise successful recovery.
    """
    return "n/a" if value is None else f"{value:.1%}"


def summarize(evaluation: dict[str, Any]) -> str:
    """A short human-readable verdict, for the CLI and the report."""
    totals = evaluation["totals"]
    lines = [
        "unit            value",
        f"documents       {totals['documents']}",
        f"pages           {totals['pages']}",
        f"pages flagged   {totals['pages_with_flags']}",
        f"text recall     {_percent(totals.get('text_recall'))}",
        f"images accounted {_percent(totals.get('image_accounting'))}",
        f"tables survived {_percent(totals.get('table_survival'))}",
        f"SCORE           {'n/a' if totals.get('score') is None else format(totals['score'], '.3f')}",
    ]
    if totals.get("flag_counts"):
        lines.append("")
        lines.append("most common problems")
        for flag, count in sorted(totals["flag_counts"].items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  {count:>5}  {flag}")
    worst = sorted(
        (doc for doc in evaluation["documents"] if not doc.get("error")),
        key=lambda doc: doc.get("score", 1.0),
    )[:5]
    if worst:
        lines.append("")
        lines.append("weakest documents")
        for doc in worst:
            lines.append(
                "  %.3f  %-46s text %.0f%%  images %.0f%%  tables %.0f%%"
                % (
                    doc["score"],
                    Path(doc["source_path"]).name[:46],
                    100 * doc["text_recall"],
                    100 * doc["image_accounting"],
                    100 * doc["table_survival"],
                )
            )
    return "\n".join(lines)
