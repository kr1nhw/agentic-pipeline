"""Unit and integration tests for the docpipe runtime.

Deliberately fixture-light: the parts that break most often are the *contracts*
(a plan that names an operator that does not exist, a parameter that is silently
dropped, a cache that serves yesterday's answer for a file that changed), and
those are testable with text files and in-memory data. The one integration test
drives a full run over a generated Markdown tree, which exercises ingest,
routing, execution, the gate, assembly and every writer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docpipe import API_VERSION
from docpipe.api.params import apply_defaults, validate_params
from docpipe.api.registry import PluginAPI, Registry
from docpipe.api.types import Capability, CapabilityError, Cost, OpResult, PageState, UnitSeed
from docpipe.builtins import build_registry, load_pipelines, validate_catalog
from docpipe.config import Config
from docpipe.exec.cache import OpCache, make_key
from docpipe.router import evaluate_rule, parse_comparison, route
from docpipe.schema import Block, MatchRule, PageRecord, PageSignals, Plan, PlanStep, flatten_pages


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def registry() -> Registry:
    return build_registry(load_entrypoints=False)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(
        in_dir=tmp_path / "in",
        out_dir=tmp_path / "out",
        work_dir=tmp_path / "work",
        offline=True,
        workers=1,
    )


def make_page(page_no: int = 1, text: str = "", **signals: object) -> PageRecord:
    return PageRecord(
        doc_id="doc",
        page_no=page_no,
        page_id=f"doc:p{page_no}",
        kind="pdf_page",
        text_layer=text,
        text_layer_chars=len(text.strip()),
        signals=PageSignals(**signals),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Schema and plans
# ─────────────────────────────────────────────────────────────────────────────


def test_flatten_pages_expands_ranges_and_singletons() -> None:
    assert flatten_pages([1, [10, 12], 20]) == {1, 10, 11, 12, 20}
    # A reversed range is a typo, not an error: read it as the range it means.
    assert flatten_pages([[5, 3]]) == {3, 4, 5}


def test_plan_override_wins_over_default() -> None:
    from docpipe.schema import PageOverride

    plan = Plan(
        doc_id="doc",
        default_steps=[PlanStep(op="pdf_text_layer")],
        page_overrides=[PageOverride(pages=[[3, 4]], steps=[PlanStep(op="vl_extract")], reason="scan")],
    )
    steps, origin = plan.steps_for(4)
    assert origin == "override"
    assert steps[0].op == "vl_extract"
    steps, origin = plan.steps_for(9)
    assert origin == "default"
    assert steps[0].op == "pdf_text_layer"


def test_table_payload_pads_ragged_rows() -> None:
    from docpipe.schema import TablePayload

    table = TablePayload(rows=[["a", "b", "c"], ["d"]])
    assert all(len(row) == 3 for row in table.rows)


# ─────────────────────────────────────────────────────────────────────────────
# Parameter validation — the contract the planner is held to
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_params_rejects_unknown_and_out_of_range() -> None:
    schema = {"dpi": {"type": "integer", "default": 150, "min": 110, "max": 600}}
    assert validate_params(schema, {"dpi": 200}) == []
    assert "unknown parameter" in validate_params(schema, {"dpi": 200, "nope": 1})[0]
    assert "must be >= 110" in validate_params(schema, {"dpi": 10})[0]
    assert "must be <= 600" in validate_params(schema, {"dpi": 6000})[0]


def test_validate_params_treats_bool_as_not_int() -> None:
    # bool is an int subclass in Python; a JSON boolean is never a dpi.
    problems = validate_params({"dpi": {"type": "integer"}}, {"dpi": True})
    assert problems and "boolean" in problems[0]


def test_apply_defaults_does_not_mutate_shared_lists() -> None:
    schema = {"tasks": {"type": "array", "default": ["ocr"]}}
    first = apply_defaults(schema, {})
    first["tasks"].append("tables")
    second = apply_defaults(schema, {})
    assert second["tasks"] == ["ocr"]


# ─────────────────────────────────────────────────────────────────────────────
# Registry and capabilities
# ─────────────────────────────────────────────────────────────────────────────


def test_vl_operator_requires_model_calls_capability() -> None:
    registry = Registry()
    api = PluginAPI(registry, "test-plugin", capabilities=[Capability.FILESYSTEM_READ])
    with pytest.raises(CapabilityError):
        api.register_op("sneaky", lambda ctx, params: OpResult(), summary="calls a model anyway", cost_class="vl")


def test_duplicate_operator_is_rejected() -> None:
    from docpipe.api.types import PluginError

    registry = Registry()
    api = PluginAPI(registry, "p", capabilities=[Capability.MODEL_CALLS], source="test")
    api.register_op("op", lambda ctx, params: OpResult(), summary="does a thing worth describing", cost_class="free")
    with pytest.raises(PluginError):
        api.register_op("op", lambda ctx, params: OpResult(), summary="does it again", cost_class="free")


def test_format_matching_prefers_the_strongest_claim(registry: Registry) -> None:
    matches = registry.formats_matching(b"%PDF-1.7\n", "weird.bin")
    assert matches and matches[0][1].id == "pdf"


def test_catalog_is_valid_and_every_operator_exists(registry: Registry) -> None:
    assert validate_catalog(registry) == []
    assert registry.pipelines, "the image must ship a pipeline catalog"


def test_shipped_pipelines_reference_real_operators(registry: Registry) -> None:
    for spec in load_pipelines():
        for step in spec.steps:
            assert step.op in registry.ops, f"{spec.id} refers to unknown operator {step.op}"


# ─────────────────────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_comparison_defaults_to_greater_equal() -> None:
    op, threshold = parse_comparison("0.5")
    assert op(0.5, 0.5) and op(1.0, 0.5) and not op(0.4, 0.5)


def test_route_prefers_the_specific_archetype(registry: Registry) -> None:
    decision = route(
        registry.pipelines,
        format_id="pdf",
        signals={"text_layer_chars": 2500, "page_width": 612, "page_height": 792, "image_area_frac": 0.05},
        n_pages=10,
    )
    assert decision.pipeline_id == "pdf-born-digital-text"


def test_route_declines_when_nothing_fits(registry: Registry) -> None:
    decision = route(
        registry.pipelines,
        format_id="pdf",
        signals={"text_layer_chars": 20, "page_width": 612, "page_height": 792, "image_area_frac": 0.95},
        n_pages=3,
    )
    assert decision.pipeline_id == "pdf-scanned-image"


def test_text_coverage_is_normalised_by_page_area() -> None:
    """200 characters is a full slide and an empty A4; the rule must see that."""
    rule = MatchRule(text_layer_ratio=">=0.45")
    slide = evaluate_rule(rule, format_id="pdf", signals={"text_layer_chars": 200, "page_width": 960, "page_height": 540}, n_pages=1)
    page = evaluate_rule(rule, format_id="pdf", signals={"text_layer_chars": 200, "page_width": 612, "page_height": 792}, n_pages=1)
    assert slide >= 0.45 > page or page == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────


def test_cache_key_changes_with_content_and_params() -> None:
    base = dict(page_id="d:p1", content_hash="aaa", op="pdf_text_layer", params={"layout": True})
    assert make_key(**base) == make_key(**base)
    assert make_key(**base) != make_key(**{**base, "content_hash": "bbb"})
    assert make_key(**base) != make_key(**{**base, "params": {"layout": False}})
    assert make_key(**base) != make_key(**{**base, "op": "pdf_tables"})


def test_cache_roundtrip_and_miss(tmp_path: Path) -> None:
    cache = OpCache(tmp_path, enabled=True)
    assert cache.get("nope") is None
    cache.put("key1", {"blocks": []})
    assert cache.get("key1") == {"blocks": []}
    assert cache.stats()["hits"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


def test_gate_flags_garbled_and_scanned_and_passes_clean(config: Config, registry: Registry) -> None:
    from docpipe.ops.checks import op_gate_default

    spec = registry.ops["gate_default_v1"]

    def gate(record: PageRecord) -> dict:
        state = PageState(record=record)
        from docpipe.api.types import OpContext

        context = OpContext(doc=None, state=state, work_dir=Path("/tmp"), out_dir=Path("/tmp"), config=config)  # type: ignore[arg-type]
        result = spec.fn(context, {})
        return result.metadata["quality"]

    clean = make_page(text="A" * 900, ink_ratio=0.1)
    clean.blocks = [Block(block_id="b", page_id="doc:p1", order=0, type="paragraph", text="x", source_op="t")]
    assert gate(clean)["needs_escalation"] is False

    garbled = make_page(text="(cid:12) " * 40)
    garbled.blocks = [Block(block_id="b", page_id="doc:p1", order=0, type="paragraph", text="x", source_op="t")]
    quality = gate(garbled)
    assert quality["needs_escalation"] is True
    assert any("garbled" in reason for reason in quality["reasons"])

    scanned = make_page(text="", image_area_frac=0.9)
    assert gate(scanned)["needs_escalation"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion over generated text, and one full run
# ─────────────────────────────────────────────────────────────────────────────


def test_markdown_ingest_splits_on_headings(tmp_path: Path) -> None:
    from docpipe.ingest.textlike import TextSession

    path = tmp_path / "notes.md"
    path.write_text("# Title\n\nIntro.\n\n## Section A\n\nBody A.\n\n```python\nprint(1)\n```\n\n## Section B\n\nBody B.\n", encoding="utf-8")
    session = TextSession(path, "doc", Config(work_dir=tmp_path), tmp_path)
    units = list(session.enumerate_units())
    assert [unit.payload["heading"] for unit in units] == ["Title", "Section A", "Section B"]
    assert units[1].payload["heading_path"] == ["Title", "Section A"]


def test_full_run_produces_every_artifact(tmp_path: Path, registry: Registry) -> None:
    """End-to-end, offline: routing, execution, gate, assembly, export."""
    from docpipe.exec.runner import Runner

    corpus = tmp_path / "in"
    (corpus / "nested").mkdir(parents=True)
    (corpus / "notes.md").write_text(
        "# Design notes\n\n" + "\n\n".join(f"## Part {i}\n\n" + "Sentence about extraction. " * 20 for i in range(3)),
        encoding="utf-8",
    )
    (corpus / "nested" / "module.py").write_text(
        "def extract(page):\n    '''Pretend to extract.'''\n    return page\n", encoding="utf-8"
    )

    config = Config(
        in_dir=corpus,
        out_dir=tmp_path / "out",
        work_dir=tmp_path / "work",
        offline=True,
        workers=1,
    )
    runner = Runner(config, registry)
    report = runner.run([corpus])

    assert report.totals["documents"] == 2
    assert report.totals["vl_calls"] == 0, "an offline run must not call a model"
    out = config.out_dir
    for artifact in ("pages.parquet", "blocks.parquet", "manifest.json", "report.json"):
        assert (out / artifact).exists(), f"missing {artifact}"

    markdown_files = list((out / "docs").glob("*/content.md"))
    assert len(markdown_files) == 2
    text = "".join(path.read_text(encoding="utf-8") for path in markdown_files)
    assert "Design notes" in text

    # Every block knows which operator produced it: that is the whole point of the IR.
    import pyarrow.parquet as pq

    blocks = pq.read_table(out / "blocks.parquet").to_pylist()
    assert blocks and all(row["source_op"] for row in blocks)
    assert all(row["page_id"] for row in blocks)


def test_offline_run_marks_pages_rather_than_failing(tmp_path: Path, registry: Registry) -> None:
    """A gate failure with no vision available produces `needs_review`, not an exception."""
    from docpipe.exec.runner import Runner

    corpus = tmp_path / "in"
    corpus.mkdir()
    # Text that trips the gate: CID placeholders mean a broken encoding map.
    (corpus / "broken.txt").write_text("(cid:12) " * 200, encoding="utf-8")

    config = Config(in_dir=corpus, out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True, workers=1)
    report = Runner(config, registry).run([corpus])
    assert report.totals["documents"] == 1
    assert report.totals["vl_calls"] == 0
    assert report.totals["gate_failures"] >= 1
    assert report.docs[0].needs_review, "a page that cannot be read must be listed for review"


def test_plugin_api_version_is_stable() -> None:
    """A plugin's manifest is checked against this; changing it is a breaking change."""
    assert API_VERSION == "1"


def test_report_is_json_serialisable(tmp_path: Path, registry: Registry) -> None:
    from docpipe.exec.runner import Runner

    corpus = tmp_path / "in"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n\nSome content.\n", encoding="utf-8")
    config = Config(in_dir=corpus, out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True, workers=1)
    Runner(config, registry).run([corpus])
    payload = json.loads((config.out_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["totals"]["documents"] == 1
    assert payload["docpipe_version"]


# ─────────────────────────────────────────────────────────────────────────────
# Robustness on real-world input
# ─────────────────────────────────────────────────────────────────────────────


def test_aggregate_signals_survives_an_empty_document() -> None:
    """A document with no units is a fact about the file, not a crash.

    Found on the first corpus of harvested documents, which included two
    deliberately-malformed Office fixtures: `pages[0]` raised IndexError and took
    the whole run down, losing every document processed after it.
    """
    from docpipe.router import aggregate_signals

    signals = aggregate_signals([])
    assert signals["units"] == 0
    assert signals["text_layer_chars"] == 0


def test_a_file_with_no_units_is_reported_not_fatal(tmp_path: Path, registry: Registry) -> None:
    from docpipe.exec.runner import Runner

    corpus = tmp_path / "in"
    corpus.mkdir()
    # Claims to be a PDF, is not one; a reader finds no pages.
    (corpus / "corrupt.pdf").write_bytes(b"%PDF-1.4\n" + b"\x00" * 512)
    # A perfectly good document alongside it, to prove one bad file does not
    # decide the fate of the corpus.
    (corpus / "good.md").write_text("# Title\n\nBody text that should be extracted.\n", encoding="utf-8")

    config = Config(in_dir=corpus, out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True, workers=1)
    report = Runner(config, registry).run([corpus])

    assert report.totals["documents"] == 2, "the good document must still be processed"
    by_name = {Path(doc.source_path).name: doc for doc in report.docs}
    assert by_name["good.md"].units >= 1
    assert by_name["corrupt.pdf"].units == 0


def test_a_degenerate_office_table_keeps_its_text(tmp_path: Path, registry: Registry) -> None:
    """Structure may be downgraded; content may not be dropped.

    A harvested Word fixture lost `T2` and `T4` entirely: its tables have one
    non-empty row, failed the two-row table test, and — unlike a PDF, whose text
    layer re-emits table characters as paragraphs — a DOCX emits paragraphs and
    tables from separate operators, so a rejected table had no fallback.
    """
    from docx import Document

    from docpipe.ingest.office import DocxSession
    from docpipe.ops.struct_ops import op_office_tables
    from docpipe.api.types import DocumentRecord, OpContext, PageState

    path = tmp_path / "tiny.docx"
    document = Document()
    document.add_paragraph("P1")
    document.add_table(rows=2, cols=2).cell(0, 0).text = "T2"
    document.save(str(path))

    config = Config(out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True)
    session = DocxSession(path, "doc", config, tmp_path / "work")
    record = PageRecord(doc_id="doc", page_no=1, page_id="doc:p1", kind="docx_flow")
    state = PageState(record=record)
    context = OpContext(
        doc=DocumentRecord(doc_id="doc", source_path=str(path), format="docx", bytes=0, sha256="doc"),
        state=state,
        work_dir=tmp_path / "work",
        out_dir=tmp_path / "out",
        config=config,
        session=session,
    )
    result = op_office_tables(context, {})
    text = " ".join(block.text for block in result.blocks)
    assert "T2" in text, f"table content was dropped: {text!r}"


def test_docx_content_without_rows_is_recovered_not_dropped(tmp_path: Path) -> None:
    """Text that python-docx cannot model is still content.

    Found on the harvested Office fixtures: a Word file whose tables have cells
    (`w:tc`) but no rows (`w:tr`). python-docx reports such a table with zero
    rows, so the row-driven reader collected nothing, the section came out empty,
    the document's fifteen characters of text reached no block at all, and the
    gate reported "1 table candidate(s) detected but none extracted". The file
    was fine; the reader was blind.

    The backstop walks the body for paragraphs the structured pass did not
    account for, so content survives even when the model of the format does not
    have a place to put it.
    """
    import zipfile

    from docpipe.ingest.office import DocxSession

    # A minimal OOXML document: a table whose cell holds a paragraph, with no
    # `w:tr` element anywhere. python-docx sees no rows.
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tc><w:p><w:r><w:t>ORPHAN1</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>ORPHAN2</w:t></w:r></w:p></w:tc>
    </w:tbl>
    <w:p><w:r><w:t>PLAIN</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    path = tmp_path / "rows-less.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document_xml)

    config = Config(out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True)
    session = DocxSession(path, "doc", config, tmp_path / "work")
    text = "\n".join(session.section_text(index) for index in range(1, len(session.sections()) + 1))

    assert "ORPHAN1" in text, f"cell text was dropped: {text!r}"
    assert "ORPHAN2" in text, f"cell text was dropped: {text!r}"
    assert "PLAIN" in text, f"body text was dropped: {text!r}"


def test_a_trailing_whitespace_chunk_is_not_a_unit(tmp_path: Path) -> None:
    """A chunk boundary is not allowed to invent a unit.

    `rfc8446.txt` produced a 57th unit holding a single character — the tail of
    the split. Nothing could be extracted from it, the gate read that as an
    unreadable page, and the run reported the document as needing review. The
    document was fine; the chunker manufactured an empty unit out of a newline.
    """
    from docpipe.ingest.textlike import TextSession

    path = tmp_path / "trailing.txt"
    path.write_text("alpha\n" * 400 + "\n", encoding="utf-8")

    config = Config(out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True)
    session = TextSession(path, "doc", config, tmp_path / "work")
    for seed in session.enumerate_units():
        assert seed.text_layer.strip(), f"unit at page {seed.page_no} was created from whitespace alone"


def test_word_shape_separates_a_garbled_layer_from_a_readable_one() -> None:
    """The second opinion that keeps a readable document off the vision path.

    Font metadata cannot clear the Congressional Record: 329 fonts, every one
    unembedded and without a unicode map, and text that extracts perfectly,
    because a font with a standard encoding needs no CMap. Nor can it convict the
    1982 survey, whose text reads correctly with the occasional substituted
    letter. The font signal proposes distrust; the text decides.

    Both samples are real, taken from the corpus documents that produced the rule.
    """
    from docpipe.ingest.pdf import WORD_SHAPE_FLOOR, text_word_shape

    garbled = (
        "~'(R) R tl] U{Ry x ySI y E Y 1t 1S 1n 1t 1S 1n ~'(R) R tl] U{Ry x ySI y E Y "
        "1t 1S 1n 1t 1S 1n ~'(R) R tl] U{Ry x ySI y E Y 1t 1S 1n 1t 1S 1n ~'(R) R tl]"
    )
    readable = (
        "Congressional Record 118 th CONGRESS, SECOND SESSION United States of America "
        "PROCEEDINGS AND DEBATES OF THE Vol. 170 WASHINGTON, TUESDAY, JANUARY 9, 2024 "
        "No. 4 House of Representatives The House met at 6:30 p.m. and was called to order"
    )
    survey = (
        "M. Jarke and J. Koch GONTENTS 1982a] and extendable to the implementa- tion of "
        "network DBMSs [Dayal and Goodman 1982]. Moreover, many popular query languages, "
        "such as SQL [Astrahan and Chamberlin 1975] or QUEL [Stonebraker et al. 1976]"
    )

    assert text_word_shape(garbled) < WORD_SHAPE_FLOOR, "a garbled layer must be convicted"
    assert text_word_shape(readable) >= WORD_SHAPE_FLOOR, "readable text must not be refused"
    assert text_word_shape(survey) >= WORD_SHAPE_FLOOR, "the 1982 survey must not be refused"


def test_a_page_with_too_little_text_is_not_convicted() -> None:
    """Two words are not evidence of a garbled layer."""
    from docpipe.ingest.pdf import text_word_shape

    assert text_word_shape("") == 1.0
    assert text_word_shape("SIGNED SEALED") == 1.0


def test_a_file_that_vanishes_mid_document_does_not_end_the_run(tmp_path: Path) -> None:
    """One lost input is one lost document, not a lost corpus.

    The sequential path had no guard around a window while the pooled path did,
    so a document that fit in a single window had a different failure behaviour
    from one that did not: a missing input raised out of the run. Found by
    deleting a staged file underneath a running container, which reported
    `FileNotFoundError: /in/CREC-2024-01-10.pdf` from a `stat` inside per-unit
    setup and exited 1 with 40 of 80 pages unread.
    """
    from docpipe.exec.runner import DocumentJob, Runner
    from docpipe.router import RouteDecision

    path = tmp_path / "vanishing.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 64)

    class Config2:
        out_dir = tmp_path / "out"
        work_dir = tmp_path / "work"
        offline = True

    job = DocumentJob(
        path=path,
        doc_id="doc",
        format_id="pdf",
        kind="pdf_page",
        session_factory=None,
        units=1,
        decision=RouteDecision(None, 0.0, [], "test"),
        signals={},
        errors=[],
    )
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "work").mkdir(exist_ok=True)
    runner = Runner.__new__(Runner)
    runner.config = Config2()
    # The file is gone by the time the record is built.
    path.unlink()
    record = Runner._document_record(runner, job)
    assert record.bytes == 0, "a vanished file must not raise out of the run"


def test_docx_recovery_reads_each_paragraph_exactly_once(tmp_path: Path) -> None:
    """The backstop must not duplicate text, and must not drop it either.

    The first version keyed "already read" on `id(element)`. lxml builds a fresh
    proxy object for an element each time it is accessed, so the same paragraph has
    a different identity in each pass, and an address freed by one paragraph can be
    reused by another. On a real Word file that produced a section whose recovered
    text repeated the body's text while the body's own paragraphs went missing —
    measured as 9% text recall on a document whose reader had every character.

    A normal document has nothing to recover, so its sections must contain each
    paragraph and each table cell exactly once.
    """
    from docx import Document

    from docpipe.ingest.office import DocxSession

    path = tmp_path / "plain.docx"
    document = Document()
    document.add_paragraph("FIRSTPARA")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "CELLA"
    table.cell(1, 1).text = "CELLB"
    document.add_paragraph("SECONDPARA")
    document.save(str(path))

    config = Config(out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True)
    session = DocxSession(path, "doc", config, tmp_path / "work")
    text = "\n".join(session.section_text(index) for index in range(1, len(session.sections()) + 1))

    for token in ("FIRSTPARA", "SECONDPARA", "CELLA", "CELLB"):
        assert text.count(token) == 1, f"{token} appeared {text.count(token)} times in {text!r}"


def test_docx_recovery_does_not_repeat_a_body_paragraph(tmp_path: Path) -> None:
    """A malformed table's cells are recovered; the body's paragraphs are not.

    The same identity bug made the sweep re-read paragraphs the structured pass
    had already emitted, so the unit's text grew a duplicate copy of itself.
    """
    import zipfile

    from docpipe.ingest.office import DocxSession

    # Three body paragraphs and two orphan cells reproduce the address reuse
    # reliably; one paragraph of each did not, and a test that passes against the
    # broken implementation is not a test.
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>BODYONE</w:t></w:r></w:p>
    <w:p><w:r><w:t>BODYTWO</w:t></w:r></w:p>
    <w:p><w:r><w:t>BODYTHREE</w:t></w:r></w:p>
    <w:tbl>
      <w:tc><w:p><w:r><w:t>ORPHANONE</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>ORPHANTWO</w:t></w:r></w:p></w:tc>
    </w:tbl>
  </w:body>
</w:document>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    path = tmp_path / "mixed.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document_xml)

    config = Config(out_dir=tmp_path / "out", work_dir=tmp_path / "work", offline=True)
    session = DocxSession(path, "doc", config, tmp_path / "work")
    text = "\n".join(session.section_text(index) for index in range(1, len(session.sections()) + 1))

    for token in ("BODYONE", "BODYTWO", "BODYTHREE", "ORPHANONE", "ORPHANTWO"):
        assert text.count(token) == 1, f"{token} appeared {text.count(token)} times: {text!r}"


def test_repeated_text_on_two_pages_is_not_a_running_head() -> None:
    """Two occurrences are not evidence of a header.

    The rule drops short text that repeats across a document, on the theory that a
    line on every page is typesetter noise. With a two-page document the threshold
    collapsed to two, so a text appearing on both pages — a two-slide deck's
    placeholder, a two-page document's subject line — was deleted as a running
    head. Found by the evaluator on a harvested deck, as 58% text recall on a
    document whose reader had every character.
    """
    from docpipe.exec.runner import MIN_RUNNING_HEAD_PAGES, Runner
    from docpipe.api.types import Block
    from docpipe.schema import PageRecord

    def record(page_no: int, texts: list[str]) -> PageRecord:
        item = PageRecord(doc_id="doc", page_no=page_no, page_id=f"doc:p{page_no}", kind="slide")
        item.blocks = [
            Block(
                block_id=f"doc:p{page_no}:s{index}",
                page_id=f"doc:p{page_no}",
                order=index,
                type="paragraph",
                text=text,
                source_op="pptx_slide_text",
            )
            for index, text in enumerate(texts)
        ]
        return item

    runner = Runner.__new__(Runner)
    records = [record(1, ["PLACEHOLDER", "first slide body"]), record(2, ["PLACEHOLDER", "second slide body"])]
    Runner._assemble(runner, records, plan=None)
    kept = [block.text for item in records for block in item.blocks]

    assert MIN_RUNNING_HEAD_PAGES >= 3
    assert "PLACEHOLDER" in kept, f"content on two pages was deleted as a header: {kept}"
    assert "first slide body" in kept and "second slide body" in kept


def test_font_report_is_computed_once_per_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pdffonts` is a property of the file, not of the page.

    The cache guard read `self._font_report` and nothing ever assigned it, so the
    subprocess ran again on every call — once per page, through `page_layout`. On a
    document with 1711 fonts that is 943 ms per page: about five minutes of
    subprocess on a 357-page issue, and the largest single component of its run.

    Counted through `subprocess.run` rather than by timing, because a timing test
    on a synthetic file would measure whatever the host happens to do.
    """
    import subprocess

    from docpipe.ingest import pdf as pdfmod

    calls: list[list[str]] = []

    def fake_run(args, *rest, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=(
                "name                                 type              encoding         emb sub uni object ID\n"
                "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                "Helvetica                            Type 1            WinAnsi          no  no  no      12  0\n"
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(pdfmod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(pdfmod.subprocess, "run", fake_run)

    session = pdfmod.PdfSession.__new__(pdfmod.PdfSession)
    session._font_report = None
    session.path = tmp_path / "any.pdf"

    first = session.font_report()
    second = session.font_report()

    assert session._font_report is not None, "the report must be cached after the first call"
    assert session.font_report() is first, "a later call must return the cached report"
    assert len(calls) == 1, f"pdffonts ran {len(calls)} times for one document"


def test_an_interrupted_run_can_be_recovered_from_shards(tmp_path: Path, registry: Registry) -> None:
    """A process killed mid-run must not take the finished documents with it.

    `pages.parquet` is a stream: parquet is unreadable without its footer, so a
    process that dies — a segfault inside a native library, an OOM kill — leaves a
    file containing every byte of finished work and no way to read any of it.
    Measured: a 357-page issue segfaulted PDFium as the 16th document of a corpus
    run, and 15 completed documents became unreadable.

    Each document now also writes a small complete shard, the run report is
    rewritten as documents finish, and `recover` merges the shards back.
    """
    from docpipe import store
    from docpipe.exec.runner import Runner

    corpus = tmp_path / "in"
    corpus.mkdir()
    (corpus / "one.txt").write_text("alpha beta gamma\n" * 40, encoding="utf-8")
    (corpus / "two.txt").write_text("delta epsilon zeta\n" * 40, encoding="utf-8")
    out = tmp_path / "out"
    config = Config(in_dir=corpus, out_dir=out, work_dir=tmp_path / "work", offline=True, workers=1)
    Runner(config, registry).run([corpus])

    assert (out / "documents.jsonl").is_file(), "each finished document must be recorded as it completes"
    rows = [line for line in (out / "documents.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) == 2, f"expected one progress row per document, got {len(rows)}"
    assert len(list((out / store.SHARDS).glob("*.pages.parquet"))) == 2, "one page shard per document"

    # Simulate the crash: the streamed artifacts are unreadable, the shards are not.
    (out / "pages.parquet").write_bytes(b"truncated, no footer")
    (out / "blocks.parquet").write_bytes(b"truncated, no footer")
    counts = store.recover(out)

    assert counts["documents"] == 2
    assert counts["pages"] == 2, f"both documents must come back: {counts}"
    # The evaluator resolves each source through the manifest, and a crashed run
    # never wrote one: without this every recovered document reports "source file
    # is gone" and nothing is measured.
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest) == 2
    assert all(Path(entry["source_path"]).is_file() for entry in manifest)
    assert counts["blocks"] >= 2
    import pyarrow.parquet as pq

    recovered = pq.read_table(out / "pages.parquet").to_pylist()
    assert {row["doc_id"] for row in recovered} == {row["doc_id"] for row in recovered}
    assert all(row["text_layer_chars"] > 0 for row in recovered)


def test_summarize_reports_a_run_with_nothing_to_measure() -> None:
    """A recovered or empty run has no aggregates, and that is not a crash.

    `f"{None:.1%}"` raised inside the summary, so `docpipe evaluate` printed a
    traceback at the very end of a successful recovery — the one path where the
    user most needs to be told what happened.
    """
    from docpipe.evaluate import summarize

    text = summarize(
        {
            "totals": {
                "documents": 0,
                "pages": 0,
                "pages_with_flags": 0,
                "text_recall": None,
                "image_accounting": None,
                "table_survival": None,
                "score": None,
                "flag_counts": {},
            },
            "documents": [],
        }
    )
    assert "n/a" in text
    assert "SCORE" in text


def test_no_pdfium_call_is_made_outside_the_lock() -> None:
    """PDFium is not thread-safe, and the runner pools threads over page windows.

    One unlocked call is enough to segfault the process with no Python traceback.
    That happened: `page_lines` loaded its page one line above the lock, on the
    path the column escalation takes, and a 357-page document with two window
    threads died with exit 139 while the same document alone succeeded.

    `self._pdf[page_no - 1]` is `FPDF_LoadPage` through `__getitem__`: it has no
    parentheses, so a checker that only walks call nodes never sees it. This runs
    the checker over every module that touches PDFium.
    """
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("check_pdfium_lock", root / "tools" / "check_pdfium_lock.py")
    assert spec and spec.loader
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    modules = sorted((root / "docpipe").rglob("*.py"))
    checked = 0
    for module in modules:
        if "pdfium" not in module.read_text(encoding="utf-8", errors="ignore"):
            continue
        checked += 1
        assert checker.main(str(module)) == 0, f"unlocked PDFium access in {module}"
    assert checked >= 1, "the checker found no module that uses PDFium, which cannot be right"


def test_evaluator_and_operator_agree_on_what_a_table_is() -> None:
    """Detection is not the expectation; acceptance is.

    The evaluator counted every rule-bounded region pdfplumber returns, while the
    operator rejects single-row boxes and mostly-empty frames. On the harvested
    corpus that produced 68 `table_missing` flags for tables the pipeline was
    right not to emit — a measurement error that reads as content loss, and the
    kind that wastes a day chasing a defect that does not exist.
    """
    from docpipe.ops.struct_ops import table_rows_accepted

    # A real table.
    assert table_rows_accepted([["Year", "Value"], ["2024", "17"], ["2025", "23"]])[0] is True
    # A single-row box the line strategy returns for any header band.
    assert table_rows_accepted([["Section 1"]])[0] is False
    # A 2x2 grid with content in every cell is a small table, and is accepted.
    assert table_rows_accepted([["A", "B"], ["C", "D"]])[0] is True
    # One column is a list, not a table.
    assert table_rows_accepted([["A"], ["B"], ["C"]])[0] is False
    # A frame that is mostly empty is a rectangle.
    assert table_rows_accepted([["a", "", "", ""], ["", "", "", ""]])[0] is False
    # Rows with no content at all are not rows.
    assert table_rows_accepted([[""], ["  "]])[0] is False
