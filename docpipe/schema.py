"""Core data contracts for docpipe.

Everything that crosses a boundary — a parquet row, a plan file, a report — is a
pydantic model here so it is validated on the way out. Hot-path runtime objects
(``OpContext``, ``OpResult``) are plain dataclasses in :mod:`docpipe.api.types`
to keep per-page overhead low.

Layering: this module imports nothing from :mod:`docpipe.ops` or
:mod:`docpipe.agent`. Semantic plan validation (does this operator exist, is this
parameter legal) lives in :mod:`docpipe.agent.validate`, which imports both.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─────────────────────────────────────────────────────────────────────────────
# Blocks — the extraction IR
# ─────────────────────────────────────────────────────────────────────────────

BlockType = Literal[
    "heading",
    "paragraph",
    "list",
    "table",
    "figure",
    "formula",
    "caption",
    "code",
    "footnote",
    "header_footer",
    "meta",
]

UnitKind = Literal["pdf_page", "slide", "docx_flow", "text_chunk", "image", "code_unit"]

Route = Literal[
    "static",
    "dynamic",
    "vl_assisted",
    "vl_only",
    "pending",
    "needs_review",
    "failed",
]


class BBox(BaseModel):
    """Page-space box in PDF points (top-left origin), the one geometry space."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)


class TablePayload(BaseModel):
    rows: list[list[str | None]] = Field(default_factory=list)
    header_rows: int = 0
    n_cols: int | None = None
    strategy: str | None = None

    @field_validator("rows")
    @classmethod
    def _rectangular(cls, rows: list[list[str | None]]) -> list[list[str | None]]:
        """Ragged rows are a bug upstream; pad rather than lose data."""
        width = max((len(r) for r in rows), default=0)
        for row in rows:
            if len(row) < width:
                row.extend([None] * (width - len(row)))
        return rows


class ImagePayload(BaseModel):
    uri: str | None = None
    width: int | None = None
    height: int | None = None
    caption: str | None = None
    media_type: str | None = None


class Block(BaseModel):
    """One content block. ``source_op`` is mandatory: a block always knows who made it."""

    block_id: str
    page_id: str
    order: int
    type: BlockType
    text: str = ""
    level: int | None = None
    table: TablePayload | None = None
    image: ImagePayload | None = None
    bbox: BBox | None = None
    source_op: str
    confidence: float = 1.0
    checks: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Pages — the streaming unit
# ─────────────────────────────────────────────────────────────────────────────


class PageSignals(BaseModel):
    """Free, deterministic per-page evidence. Filled in tiers; never guessed."""

    model_config = ConfigDict(extra="allow")

    text_layer_chars: int = 0
    ink_ratio: float | None = None
    image_area_frac: float | None = None
    n_images: int = 0
    has_table_candidate: bool = False
    n_table_candidates: int = 0
    font_sizes: list[float] = Field(default_factory=list)
    n_lines: int = 0
    encoding_suspect: bool = False
    page_width: float | None = None
    page_height: float | None = None
    layout_hint: str | None = None
    text_sample: str | None = None
    #: Estimated column count. 1 for prose, 2+ when a gutter was measured.
    columns: int = 1
    #: How much the text layer can be believed, 0..1. Low means it is an OCR
    #: layer: present, plausible, and wrong about characters.
    text_layer_trust: float = 1.0


class PageRecord(BaseModel):
    """One row of ``pages.parquet`` — mutable while a unit is being processed."""

    model_config = ConfigDict(validate_assignment=False)

    doc_id: str
    page_no: int
    page_id: str
    kind: UnitKind
    signals: PageSignals = Field(default_factory=PageSignals)
    text_layer: str | None = None
    text_layer_chars: int = 0
    raster_uri: str | None = None
    plan: dict[str, Any] | None = None
    blocks: list[Block] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)
    route: Route = "pending"
    pipeline_id: str | None = None
    vl_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd_est: float = 0.0
    attempt: int = 0
    duration_ms: int = 0
    errors: list[str] = Field(default_factory=list)
    provenance: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def text(self) -> str:
        """Reading-order text of the assembled blocks."""
        return "\n\n".join(b.text for b in sorted(self.blocks, key=lambda b: b.order) if b.text)


# ─────────────────────────────────────────────────────────────────────────────
# Plans — what the router picks and the agent composes
# ─────────────────────────────────────────────────────────────────────────────

PageSel = int | list[int]


class PlanStep(BaseModel):
    op: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None
    reason: str | None = None


class Scope(BaseModel):
    """Which pages a plan's ``default_steps`` apply to. ``all`` or explicit pages."""

    pages: Literal["all"] | list[PageSel] = "all"


class PageOverride(BaseModel):
    pages: list[PageSel]
    steps: list[PlanStep]
    reason: str = ""
    replace: bool = True


class Assembly(BaseModel):
    order: str = "reading_order_v1"
    drop: list[str] = Field(default_factory=list)


class VerifySpec(BaseModel):
    #: Must name a registered operator: the runner looks this up directly, and a
    #: default that does not exist made every degraded plan report "unknown gate"
    #: instead of gating anything.
    gate: str = "gate_default_v1"
    escalate_to: str | None = "vl_extract"
    max_escalations: int = 1
    cross_check_sample: float = 0.0


class BudgetSpec(BaseModel):
    max_vl_calls: int | None = None
    max_tokens: int | None = None
    max_usd: float | None = None


class Plan(BaseModel):
    """A validated pipeline for one document. Static catalog entries and dynamic
    agent output both lower to exactly this type, so the executor has one input."""

    schema_version: str = "1"
    doc_id: str
    pipeline_id: str | None = None
    pipeline_hash: str | None = None
    scope: Scope = Field(default_factory=Scope)
    default_steps: list[PlanStep] = Field(default_factory=list)
    page_overrides: list[PageOverride] = Field(default_factory=list)
    assembly: Assembly = Field(default_factory=Assembly)
    verify: VerifySpec = Field(default_factory=VerifySpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    notes: str = ""
    plan_degraded: bool = False

    def steps_for(self, page_no: int) -> tuple[list[PlanStep], str]:
        """Return the steps for a page and where they came from."""
        for override in self.page_overrides:
            if page_no in flatten_pages(override.pages):
                return override.steps, "override"
        return self.default_steps, "default"

    def vl_call_budget(self) -> int | None:
        return self.budget.max_vl_calls


def flatten_pages(selectors: list[PageSel]) -> set[int]:
    """Expand ``[1, [10, 12], 20]`` into ``{1, 10, 11, 12, 20}``."""
    out: set[int] = set()
    for sel in selectors:
        if isinstance(sel, int):
            out.add(int(sel))
        elif isinstance(sel, (list, tuple)) and len(sel) == 2:
            start, end = int(sel[0]), int(sel[1])
            if end < start:
                start, end = end, start
            out.update(range(start, end + 1))
        else:  # pragma: no cover - shape validation rejects this earlier
            raise ValueError(f"bad page selector: {sel!r}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pipelines — the static catalog
# ─────────────────────────────────────────────────────────────────────────────


class MatchRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: str | list[str] | None = None
    # Comparison rules are strings like ">=0.85" so the YAML stays declarative.
    text_layer_ratio: str | None = None
    image_area_frac: str | None = None
    pages: str | None = None
    layout_hint: str | None = None
    min_confidence: float = 0.6


class PipelineSpec(BaseModel):
    id: str
    api_version: str = "1"
    version: str = "1"
    title: str = ""
    description: str = ""
    match: MatchRule = Field(default_factory=MatchRule)
    steps: list[PlanStep] = Field(default_factory=list)
    assembly: Assembly = Field(default_factory=Assembly)
    verify: VerifySpec = Field(default_factory=VerifySpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


# ─────────────────────────────────────────────────────────────────────────────
# Documents and reports
# ─────────────────────────────────────────────────────────────────────────────


class DocumentRecord(BaseModel):
    doc_id: str
    source_path: str
    format: str
    bytes: int
    sha256: str
    units: int = 0
    kind: UnitKind = "pdf_page"
    probe: dict[str, Any] = Field(default_factory=dict)
    pipeline_id: str | None = None
    errors: list[str] = Field(default_factory=list)


class DocReport(BaseModel):
    doc_id: str
    source_path: str
    format: str
    units: int = 0
    route_counts: dict[str, int] = Field(default_factory=dict)
    gate_failures: int = 0
    escalations: int = 0
    vl_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd_est: float = 0.0
    pipeline_id: str | None = None
    needs_review: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class RunReport(BaseModel):
    docpipe_version: str
    api_version: str
    started_at: datetime
    finished_at: datetime | None = None
    model: str | None = None
    offline: bool = False
    engine: str = "local"
    shard: str | None = None
    docs: list[DocReport] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    plugins: list[dict[str, Any]] = Field(default_factory=list)
    stopped_reason: str | None = None

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
