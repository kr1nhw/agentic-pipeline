"""Runtime types shared by operators, the executor and plugins.

These are deliberately plain dataclasses: they are constructed once per page per
step, so pydantic validation belongs at the artifact boundary
(:mod:`docpipe.schema`), not here.

The two capabilities an operator may want that are not free — a raster and a
model call — arrive through :class:`OpContext` as protocols, so the executor can
rate-limit, cache and budget them in one place instead of trusting each operator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol, runtime_checkable

from docpipe.schema import (
    BBox,
    Block,
    DocumentRecord,
    ImagePayload,
    PageRecord,
    PageSignals,
    TablePayload,
    UnitKind,
)

CostClass = Literal["free", "local", "vl"]


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────


class DocpipeError(RuntimeError):
    """Base class for every error this package raises deliberately."""


class OpError(DocpipeError):
    """An operator failed on one unit. The unit is marked failed; the run continues."""


class PlanValidationError(DocpipeError):
    """A plan is semantically invalid. Raised with every problem, not just the first."""


class PluginError(DocpipeError):
    """A plugin could not be loaded, registered or trusted."""


class CapabilityError(DocpipeError):
    """A plugin tried something it did not declare. Recorded as a violation."""


class BudgetExceeded(DocpipeError):
    """A hard budget stop. Signals a clean, resumable stop, not a crash."""


# ─────────────────────────────────────────────────────────────────────────────
# Plugin capabilities
# ─────────────────────────────────────────────────────────────────────────────


class Capability(str, Enum):
    """What a plugin may do. Declared in its manifest and enforced by proxy."""

    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    SUBPROCESS = "subprocess"
    NETWORK = "network"
    MODEL_CALLS = "model_calls"


# ─────────────────────────────────────────────────────────────────────────────
# Service protocols the executor implements
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class SupportsRaster(Protocol):
    """Lazy page rasterisation. Backed by whatever the format's ops produced."""

    def raster(self, page_no: int, dpi: int, *, force: bool = False) -> Path: ...

    def raster_path(self, page_no: int, dpi: int) -> Path: ...


@runtime_checkable
class SupportsVL(Protocol):
    """The gated model client. Every call is metered and budget-checked."""

    def complete(self, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class SupportsCache(Protocol):
    """Content-addressed op cache: same key ⇒ same result, no work."""

    def get(self, key: str) -> Any | None: ...

    def put(self, key: str, value: Any) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# Cost accounting
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Cost:
    vl_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    usd: float = 0.0
    duration_ms: int = 0

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(
            vl_calls=self.vl_calls + other.vl_calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            usd=self.usd + other.usd,
            duration_ms=self.duration_ms + other.duration_ms,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "vl_calls": self.vl_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "usd": round(self.usd, 6),
            "duration_ms": self.duration_ms,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Operator results and page state
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class OpResult:
    """What an operator returns. The executor merges it into the page state."""

    blocks: list[Block] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    cost: Cost = field(default_factory=Cost)
    ok: bool = True
    diagnostics: list[str] = field(default_factory=list)
    #: A later, better extraction replaces earlier blocks (e.g. VL over garbage text)
    #: instead of appending to them.
    replace_blocks: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failed(cls, message: str) -> "OpResult":
        return cls(ok=False, diagnostics=[message])


#: Block types that a text-replacement operator supersedes. Structured and visual
#: blocks are NOT prose: a table, a figure or a caption is not what a text pass
#: re-reads, so replacing prose must leave them in place.
PROSE_TYPES = frozenset({"paragraph", "heading", "list"})


@dataclass
class PageState:
    """Mutable working state for one unit as it flows through a plan's steps."""

    record: PageRecord
    artifacts: dict[str, str] = field(default_factory=dict)
    scratch: dict[str, Any] = field(default_factory=dict)
    costs: Cost = field(default_factory=Cost)

    @property
    def page_id(self) -> str:
        return self.record.page_id

    @property
    def page_no(self) -> int:
        return self.record.page_no

    @property
    def blocks(self) -> list[Block]:
        return self.record.blocks

    def merge(self, result: OpResult, source_op: str) -> None:
        """Fold an operator result into this page, preserving block order.

        `replace_blocks` means "my text supersedes the text on this page", not "the
        page contains nothing else". It cleared every block, so an operator that
        re-reads a page in reading order discarded the tables, figures and captions
        the plan had already extracted — and the pages that take that path are
        exactly the ones whose plan had found tables.

        Measured on the harvested corpus: 41 pages reported `table_missing`, every
        one of them on a page whose route was `adapted`. On those pages the table
        operator had run and reported `tables_found: 1`; the column pass then threw
        the table away. Content that was never extracted is a coverage gap.
        Content that was extracted and then discarded is a bug.
        """
        if result.replace_blocks:
            self.record.blocks = [block for block in self.record.blocks if block.type not in PROSE_TYPES]
        next_order = max((b.order for b in self.record.blocks), default=-1) + 1
        for block in result.blocks:
            block.order = next_order
            block.page_id = self.record.page_id
            if not block.source_op:
                block.source_op = source_op
            next_order += 1
            self.record.blocks.append(block)
        if result.signals:
            for key, value in result.signals.items():
                setattr(self.record.signals, key, value)
        if result.artifacts:
            self.artifacts.update(result.artifacts)
        # Diagnostics from a SUCCESSFUL operator are notes, not errors: the vision
        # path reports what it spent, and pdfplumber reports rejected tables, both
        # of which are useful and neither of which is a failure. Recording them as
        # errors made `report.json` say "2 documents reported errors" on a run
        # where nothing failed, which is the fastest way to teach a reader to
        # ignore the field.
        if result.diagnostics and not result.ok:
            self.record.errors.extend(result.diagnostics)

    def add_provenance(self, op: str, params: dict[str, Any], cost: Cost, **extra: Any) -> None:
        entry: dict[str, Any] = {
            "op": op,
            "params": params,
            "cost": cost.as_dict(),
        }
        entry.update(extra)
        self.record.provenance.append(entry)
        self.costs = self.costs + cost
        self.record.vl_calls += cost.vl_calls
        self.record.prompt_tokens += cost.prompt_tokens
        self.record.completion_tokens += cost.completion_tokens
        self.record.usd_est += cost.usd


# ─────────────────────────────────────────────────────────────────────────────
# Contexts
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FormatContext:
    """Handed to ``enumerate_units`` while ingest streams a document."""

    path: Path
    doc_id: str
    work_dir: Path
    log: Callable[..., None] = lambda *a, **k: None
    scratch: dict[str, Any] = field(default_factory=dict)


@dataclass
class UnitSeed:
    """One unit discovered by a format plugin.

    Enumeration is streaming and cheap-first: a PDF yields page numbers while
    filling the text layer as it goes, so the router can decide without the page
    ever being rasterised.
    """

    page_no: int
    kind: UnitKind
    signals: PageSignals | None = None
    text_layer: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_record(self, doc_id: str) -> PageRecord:
        text = self.text_layer or ""
        return PageRecord(
            doc_id=doc_id,
            page_no=self.page_no,
            page_id=f"{doc_id}:p{self.page_no}",
            kind=self.kind,
            signals=self.signals or PageSignals(),
            text_layer=self.text_layer,
            text_layer_chars=len(text),
        )


@dataclass
class OpContext:
    """Everything an operator may use. Shared services are the only way out."""

    doc: DocumentRecord
    state: PageState
    work_dir: Path
    out_dir: Path
    config: Any
    cache: SupportsCache | None = None
    budget: Any | None = None
    vl: SupportsVL | None = None
    rasterer: SupportsRaster | None = None
    #: The open document session. Operators that need structure beyond the text
    #: layer (fonts, lines, tables, shapes) read it here rather than reopening
    #: the file, which is what keeps a 1428-page document affordable.
    session: Any | None = None
    log: Callable[..., None] = lambda *a, **k: None
    #: Scratch directory unique to this document, for rasters and extracted media.
    doc_dir: Path | None = None
    #: Capabilities of the plugin that owns the operator being run. Used to gate
    #: subprocess use; the model path is gated by passing `vl` only when allowed.
    capabilities: frozenset[Any] = frozenset()

    @property
    def offline(self) -> bool:
        return bool(getattr(self.config, "offline", False))

    def cache_key(self, op: str, params: dict[str, Any], *extra: Any) -> str:
        """Build the content-addressed key for this unit and operator."""
        from docpipe.exec.cache import make_key

        return make_key(
            page_id=self.state.page_id,
            content_hash=self.state.record.signals.model_extra.get("content_hash", ""),
            op=op,
            params=params,
            extra=extra,
        )

    def raster(self, page_no: int | None = None, dpi: int = 150, *, force: bool = False) -> Path:
        if self.rasterer is None:
            raise OpError("rasterisation is unavailable for this document")
        return self.rasterer.raster(page_no if page_no is not None else self.state.page_no, dpi, force=force)

    def raster_path(self, page_no: int | None = None, dpi: int = 150) -> Path:
        if self.rasterer is None:
            raise OpError("rasterisation is unavailable for this document")
        return self.rasterer.raster_path(page_no if page_no is not None else self.state.page_no, dpi)


# ─────────────────────────────────────────────────────────────────────────────
# Registry entries
# ─────────────────────────────────────────────────────────────────────────────


OpFn = Callable[[OpContext, dict[str, Any]], OpResult]


@dataclass(frozen=True)
class OpSpec:
    """A registered operator. The summary is what the planner reads, verbatim."""

    name: str
    fn: OpFn
    summary: str
    params_schema: dict[str, Any] = field(default_factory=dict)
    cost_class: CostClass = "free"
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    backends: tuple[str, ...] = ()
    plugin: str = "builtin"
    unit_kinds: tuple[UnitKind, ...] = ()

    def schema_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "cost_class": self.cost_class,
            "params": self.params_schema,
            "requires": list(self.requires),
            "produces": list(self.produces),
            "backends": list(self.backends),
        }


EnumerateFn = Callable[[FormatContext], Iterator[UnitSeed]]
MatchFn = Callable[[bytes, str], float]
SessionFactory = Callable[[Path, str, Any], Any]


@dataclass(frozen=True)
class FormatSpec:
    """A registered input format.

    ``seekable`` and ``streaming`` are promises the runtime relies on when it
    shards work and bounds memory: a seekable format can be split by unit without
    reading what came before, and a streaming one can be consumed once.

    ``session_factory`` opens the per-worker session that owns the file handle.
    It is optional: a format with no session is extractable but cannot rasterise,
    so every ``vl_*`` operator is unavailable to it.
    """

    id: str
    match: MatchFn
    enumerate_units: EnumerateFn
    kind: UnitKind
    default_ops: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    seekable: bool = True
    streaming: bool = False
    max_unit_bytes: int | None = None
    plugin: str = "builtin"
    session_factory: SessionFactory | None = None


@dataclass
class HookFn:
    point: str
    fn: Callable[..., Any]
    priority: int = 100
    plugin: str = "builtin"


@dataclass
class Timer:
    """Wall-clock helper so every op reports its own duration."""

    started: float = field(default_factory=time.perf_counter)

    def ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)
