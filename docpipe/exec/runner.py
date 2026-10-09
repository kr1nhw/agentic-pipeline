"""The runner: discovery, routing, per-unit execution, escalation, assembly.

Shape of a run:

1. **Detect** each input's format from its bytes, not its extension.
2. **Probe** — enumerate units and collect *free* signals only. Text is dropped
   after each unit, so probing a 1428-page standard costs signals, not memory.
3. **Route** the document to a static pipeline. A confident match runs with no
   model calls at all; no match is a first-class outcome that the dynamic planner
   handles, not a fallback to a default pipeline chosen by accident.
4. **Execute** the plan per unit, then run the gate. Only a gate failure reaches
   the escalation ladder, and the ladder is bounded.
5. **Assemble** the document (running heads out, chunks in) and stream artifacts
   to disk.

Work is fanned out as *page windows*, each opening its own session. That single
choice gives parallelism within one huge document and across a corpus, keeps
per-worker memory bounded to a window, and means a killed worker loses a window
rather than a document.
"""

from __future__ import annotations

import concurrent.futures as futures
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from docpipe import API_VERSION, __version__
from docpipe.agent.vl import Budget, VLClient
from docpipe.api.params import apply_defaults, validate_params
from docpipe.api.registry import Registry
from docpipe.api.types import (
    BudgetExceeded,
    Capability,
    CapabilityError,
    Cost,
    DocpipeError,
    OpContext,
    OpResult,
    PageState,
    UnitSeed,
)
from docpipe.config import Config
from docpipe.exec.cache import OpCache, make_key
from docpipe.ingest.base import SNIFF_BYTES, doc_id_for, sniff
from docpipe.router import RouteDecision, aggregate_signals, route_document
from docpipe.schema import (
    Block,
    DocReport,
    DocumentRecord,
    PageRecord,
    Plan,
    PlanStep,
    RunReport,
    UnitKind,
)
from docpipe import store

#: Pages per work unit. Small enough to parallelise a huge document, large enough
#: that per-window session setup disappears into the noise.
WINDOW_PAGES = 200

#: A document larger than this is probed by sampling rather than exhaustively.
PROBE_FULL_LIMIT = 300

#: How many pages a sampled probe reads.
PROBE_SAMPLE = 60

#: Running heads repeat on this fraction of pages before they are treated as such.
RUNNING_HEAD_FRACTION = 0.7


@dataclass
class DocumentJob:
    """One input file, resolved to a format and a route."""

    path: Path
    doc_id: str
    format_id: str
    kind: UnitKind
    session_factory: Any
    units: int
    decision: RouteDecision
    signals: dict[str, Any]
    plan: Plan | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class UnitOutcome:
    record: PageRecord
    chunks: list[dict[str, Any]] = field(default_factory=list)


class Runner:
    def __init__(
        self,
        config: Config,
        registry: Registry,
        *,
        vl: VLClient | None = None,
        budget: Budget | None = None,
        log: Any = None,
    ) -> None:
        self.config = config
        self.registry = registry
        self.log = log or (lambda *args, **kwargs: None)
        self.budget = budget or Budget(config)
        self.vl = vl
        if self.vl is not None:
            self.vl.budget = self.budget
        self.cache = OpCache(config.cache_dir, enabled=not config.no_cache)
        self._lock = threading.Lock()
        self._stopped: str | None = None

    # ── entry point ─────────────────────────────────────────────────────────

    def discover(self, inputs: Sequence[Path]) -> list[Path]:
        files: list[Path] = []
        for entry in inputs:
            path = Path(entry)
            if path.is_dir():
                files.extend(
                    sorted(
                        child
                        for child in path.rglob("*")
                        if child.is_file() and not child.name.startswith(".")
                    )
                )
            elif path.is_file():
                files.append(path)
        return files

    def run(self, inputs: Sequence[Path]) -> RunReport:
        """Process every input, streaming artifacts as documents complete."""
        self.config.ensure_dirs()
        started = store.RunReport.now()

        files = self.discover(inputs)
        if not files:
            self.log("no_inputs", inputs=[str(item) for item in inputs])

        jobs: list[DocumentJob] = []
        for path in files:
            try:
                job = self._prepare(path)
            except DocpipeError as exc:
                self.log("prepare_failed", path=str(path), error=str(exc))
                continue
            if job is not None:
                jobs.append(job)

        if self.config.shard:
            jobs = self._apply_shard(jobs)

        docs: list[DocReport] = []
        page_stream = store.ParquetStream(self.config.out_dir / "pages.parquet", store.page_schema())
        block_stream = store.ParquetStream(self.config.out_dir / "blocks.parquet", store.block_schema())
        try:
            for job in jobs:
                if self.budget.exhausted:
                    break
                try:
                    doc_report = self._run_document(job, page_stream, block_stream)
                except BudgetExceeded as exc:
                    self._stopped = str(exc)
                    break
                except DocpipeError as exc:
                    doc_report = DocReport(
                        doc_id=job.doc_id,
                        source_path=str(job.path),
                        format=job.format_id,
                        units=job.units,
                        errors=[f"{type(exc).__name__}: {exc}"],
                    )
                docs.append(doc_report)
        finally:
            page_stream.close()
            block_stream.close()

        manifest = [self._document_record(job).model_dump() for job in jobs]
        store.write_json(self.config.out_dir / store.MANIFEST, manifest)

        report = store.summarize_report(
            version=__version__,
            api_version=API_VERSION,
            docs=docs,
            started=started,
            finished=store.RunReport.now(),
            budget={**self.budget.snapshot(), "cache": self.cache.stats()},
            plugins=self.registry.plugin_summary(),
            model=getattr(self.config, "model", None) if self.config.model_enabled else None,
            offline=not self.config.model_enabled,
            engine=self.config.engine,
            shard=self.config.shard,
            stopped_reason=self._stopped or self.budget.stopped_reason,
        )
        store.write_json(self.config.out_dir / store.REPORT, report.model_dump(mode="json"))
        return report

    # ── sharding ────────────────────────────────────────────────────────────

    def _apply_shard(self, jobs: list[DocumentJob]) -> list[DocumentJob]:
        """Deterministic partition over (document, window) work units.

        Hash-based, not round-robin, so a shard's membership does not depend on
        how many other files happen to be present.
        """
        assert self.config.shard is not None
        try:
            index_text, count_text = self.config.shard.split("/", 1)
            index, count = int(index_text), int(count_text)
        except (ValueError, AttributeError):
            raise DocpipeError(f"--shard must look like 'i/N', got {self.config.shard!r}") from None
        if not 0 <= index < count:
            raise DocpipeError(f"--shard index {index} is outside 0..{count - 1}")

        kept: list[DocumentJob] = []
        for job in jobs:
            digest = int(job.doc_id[:8], 16)
            if digest % count == index:
                kept.append(job)
        self.log("shard", index=index, count=count, documents=len(kept))
        return kept

    # ── preparation ─────────────────────────────────────────────────────────

    def _prepare(self, path: Path) -> DocumentJob | None:
        head = sniff(path)
        matches = self.registry.formats_matching(head, str(path))
        if not matches:
            self.log("unknown_format", path=str(path))
            return None
        score, spec = matches[0]
        doc_id = doc_id_for(path)
        session_factory = spec.session_factory
        if session_factory is None:
            self.log("format_without_session", path=str(path), format=spec.id)
            return None

        session = session_factory(path, doc_id, self.config)
        try:
            units = session.units()
            signals, _ = self._probe(session, units)
        finally:
            session.close()

        decision = route_document(
            self.registry.pipelines,
            format_id=spec.id,
            pages=signals["pages"],
            min_confidence=0.6,
        )
        return DocumentJob(
            path=path,
            doc_id=doc_id,
            format_id=spec.id,
            kind=spec.kind,
            session_factory=session_factory,
            units=units,
            decision=decision,
            signals=signals["aggregate"],
        )

    def _probe(self, session: Any, units: int) -> tuple[dict[str, Any], list[PageRecord]]:
        """Collect free signals only.

        Text is dropped immediately: routing needs shapes and counts, never a
        corpus in memory. Very large documents are sampled, because a 1428-page
        standard's archetype is decided by its first pages and its page geometry,
        not by reading all of it twice.
        """
        pages: list[PageRecord] = []
        step = 1
        limit = units
        if units > PROBE_FULL_LIMIT:
            step = max(1, units // PROBE_SAMPLE)
            limit = units

        for seed in session.enumerate_units():
            if seed.page_no % step != 0 and seed.page_no not in (1, units):
                continue
            record = seed.to_record(session.doc_id)
            record.text_layer = None  # signals only
            pages.append(record)
            if len(pages) >= PROBE_SAMPLE and units > PROBE_FULL_LIMIT:
                break
        return {"pages": pages, "aggregate": aggregate_signals(pages)}, pages

    def _document_record(self, job: DocumentJob) -> DocumentRecord:
        stat = job.path.stat()
        return DocumentRecord(
            doc_id=job.doc_id,
            source_path=str(job.path),
            format=job.format_id,
            bytes=stat.st_size,
            sha256=job.doc_id,
            units=job.units,
            kind=job.kind,
            probe={"route": job.decision.pipeline_id, "confidence": job.decision.confidence, "reason": job.decision.reason},
        )

    # ── document execution ──────────────────────────────────────────────────

    def _plan_for(self, job: DocumentJob) -> Plan:
        """Resolve the document's plan: catalog entry, agent plan, or degraded default."""
        if job.plan is not None:
            return job.plan
        if job.decision.confident and job.decision.pipeline_id:
            spec = self.registry.pipelines[job.decision.pipeline_id]
            return Plan(
                doc_id=job.doc_id,
                pipeline_id=spec.id,
                default_steps=list(spec.steps),
                assembly=spec.assembly,
                verify=spec.verify,
                budget=spec.budget,
                notes=job.decision.reason,
            )

        # No archetype fits. Offline, fall back to the format's declared default
        # operators and mark the plan degraded so the report shows it plainly.
        default_ops = self.registry.formats.get(job.format_id)
        steps = [PlanStep(op=name) for name in (default_ops.default_ops if default_ops else ())]
        return Plan(
            doc_id=job.doc_id,
            default_steps=steps,
            plan_degraded=True,
            notes=f"no archetype matched ({job.decision.reason}); using format defaults",
        )

    def _run_document(self, job: DocumentJob, page_stream: Any, block_stream: Any) -> DocReport:
        started = time.monotonic()
        plan = self._plan_for(job)
        self.log(
            "document_start",
            doc_id=job.doc_id,
            path=str(job.path),
            format=job.format_id,
            units=job.units,
            pipeline=plan.pipeline_id,
            degraded=plan.plan_degraded,
        )

        windows = [
            (start, min(job.units, start + WINDOW_PAGES - 1))
            for start in range(1, job.units + 1, WINDOW_PAGES)
        ]
        outcomes: list[UnitOutcome] = []
        workers = max(1, self.config.effective_workers)
        if workers == 1 or len(windows) == 1:
            for start, end in windows:
                outcomes.extend(self._run_window(job, plan, start, end))
        else:
            with futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="docpipe") as pool:
                pending = {
                    pool.submit(self._run_window, job, plan, start, end): (start, end)
                    for start, end in windows
                }
                for future in futures.as_completed(pending):
                    start, end = pending[future]
                    try:
                        outcomes.extend(future.result())
                    except Exception as exc:  # noqa: BLE001 - a window failure is reported, not fatal
                        self.log("window_failed", doc_id=job.doc_id, start=start, end=end, error=str(exc))

        outcomes.sort(key=lambda outcome: outcome.record.page_no)
        records = [outcome.record for outcome in outcomes]
        self._assemble(records, plan)

        # Chunking is an export concern, not an extraction decision: the plan
        # chooses how to read a page, the exporter chooses how to package it.
        chunks: list[dict[str, Any]] = []
        if self.config.engine != "off" and records:
            for outcome in outcomes:
                chunks.extend(outcome.chunks or [])
            if not chunks:
                chunks = self._chunk_pages(job, plan, records)

        for record in records:
            page_stream.write(store.page_row(record))
            for row in store.block_rows(record):
                block_stream.write(row)
        page_stream.flush()
        block_stream.flush()

        document = self._document_record(job)
        document.pipeline_id = plan.pipeline_id or ("degraded" if plan.plan_degraded else None)
        store.write_document_markdown(self.config.out_dir, document, records)
        store.write_document_jsonl(self.config.out_dir, document, records)
        if chunks:
            store.append_jsonl(store.doc_dir(self.config.out_dir, job.doc_id) / "chunks.jsonl", chunks)

        report = self._doc_report(job, plan, records, chunks, started)
        self.log(
            "document_done",
            doc_id=job.doc_id,
            units=len(records),
            vl_calls=report.vl_calls,
            gate_failures=report.gate_failures,
            needs_review=len(report.needs_review),
            seconds=round(time.monotonic() - started, 1),
        )
        return report

    def _run_window(self, job: DocumentJob, plan: Plan, start: int, end: int) -> list[UnitOutcome]:
        """Process a contiguous page window with its own session."""
        session = job.session_factory(job.path, job.doc_id, self.config)
        outcomes: list[UnitOutcome] = []
        try:
            seeds = [seed for seed in session.enumerate_units() if start <= seed.page_no <= end]
            for seed in seeds:
                if self.budget.exhausted:
                    break
                outcome = self._run_unit(job, plan, session, seed)
                outcomes.append(outcome)
        finally:
            session.close()
        return outcomes

    # ── unit execution ──────────────────────────────────────────────────────

    def _run_unit(self, job: DocumentJob, plan: Plan, session: Any, seed: UnitSeed) -> UnitOutcome:
        started = time.monotonic()
        record = seed.to_record(job.doc_id)
        record.pipeline_id = plan.pipeline_id
        state = PageState(record=record)
        steps, origin = plan.steps_for(seed.page_no)
        record.route = "static" if origin == "default" and not plan.plan_degraded else "dynamic"

        escalations = 0
        attempt_steps = list(steps)
        while True:
            self._execute_steps(job, plan, session, state, attempt_steps)
            quality = self._gate(job, plan, session, state)
            record.quality = quality
            if not quality.get("needs_escalation"):
                break
            if escalations >= max(0, plan.verify.max_escalations):
                record.route = "needs_review"
                break
            if not self._can_escalate(plan):
                record.route = "needs_review" if quality.get("needs_escalation") else record.route
                break
            escalations += 1
            attempt_steps = self._escalation_steps(plan, quality, escalations)
            if not attempt_steps:
                record.route = "needs_review"
                break
            record.route = "vl_assisted" if record.route == "static" else record.route

        if record.vl_calls and not record.blocks:
            record.route = "failed"
        record.duration_ms = int((time.monotonic() - started) * 1000)
        record.raster_uri = state.artifacts.get("raster") or (record.signals.model_extra or {}).get("raster_uri")
        chunks = state.scratch.get("chunks") or []
        return UnitOutcome(record=record, chunks=chunks)

    def _execute_steps(
        self,
        job: DocumentJob,
        plan: Plan,
        session: Any,
        state: PageState,
        steps: Iterable[PlanStep],
    ) -> None:
        for step in steps:
            try:
                spec = self.registry.get_op(step.op)
            except DocpipeError as exc:
                state.record.errors.append(f"plan refers to {step.op!r}: {exc}")
                continue
            params = apply_defaults(spec.params_schema, step.params)
            problems = validate_params(spec.params_schema, params)
            if problems:
                state.record.errors.append(f"{step.op}: {'; '.join(problems)}")
                continue
            if spec.cost_class == "vl" and not self._vl_allowed(spec, state):
                state.record.errors.append(f"{step.op}: skipped (vision unavailable or disallowed)")
                continue

            context = self._context(job, session, state, spec)
            cache_key = make_key(
                page_id=state.page_id,
                content_hash=(state.record.signals.model_extra or {}).get("content_hash", job.doc_id),
                op=spec.name,
                params=params,
                extra=(spec.plugin, spec.cost_class, getattr(self.config, "model", "")),
            )
            cached = self.cache.get(cache_key)
            if cached is not None and spec.cost_class == "vl":
                # Never re-buy a vision answer we already have.
                self._apply_cached(state, cached, spec.name)
                continue

            try:
                result = spec.fn(context, params)
            except CapabilityError:
                raise
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad operator is one bad page
                state.record.errors.append(f"{step.op} raised {type(exc).__name__}: {exc}")
                continue

            if not isinstance(result, OpResult):
                state.record.errors.append(f"{step.op} returned {type(result).__name__}, expected OpResult")
                continue
            state.merge(result, spec.name)
            state.add_provenance(spec.name, params, result.cost, ok=result.ok, origin="escalation" if cached is None else "run")
            if result.metadata.get("chunks"):
                state.scratch["chunks"] = result.metadata["chunks"]
            if result.metadata.get("quality"):
                state.record.quality = result.metadata["quality"]
            if spec.cost_class == "vl" and result.ok:
                self.cache.put(cache_key, _serialize_result(result))

    def _apply_cached(self, state: PageState, cached: dict[str, Any], op: str) -> None:
        """Replay a cached result so a warm run behaves exactly like a cold one.

        `replace_blocks` has to travel with the entry. Without it, a cached
        `vl_extract` appended its blocks to the garbage it was supposed to
        supersede, the gate then flagged the page again, and the same document
        that succeeded cold came back `needs_review` warm — from the cache that
        exists precisely to make runs identical.
        """
        blocks = [Block.model_validate(item) for item in cached.get("blocks", [])]
        state.merge(
            OpResult(
                blocks=blocks,
                signals=cached.get("signals") or {},
                replace_blocks=bool(cached.get("replace_blocks", False)),
                cost=Cost(),
            ),
            op,
        )
        state.add_provenance(op, {}, Cost(), ok=True, origin="cache")

    def _context(self, job: DocumentJob, session: Any, state: PageState, spec: Any) -> OpContext:
        capabilities = self.registry.capabilities_for(spec.plugin)
        # A vl-class operator from a plugin that never declared model_calls has no
        # client at all — the money path is closed by construction, not by trust.
        vl = self.vl if (spec.cost_class == "vl" and Capability.MODEL_CALLS in capabilities) else None
        if vl is not None and not self.config.model_enabled:
            vl = None
        return OpContext(
            doc=self._document_record(job),
            state=state,
            work_dir=self.config.work_dir,
            out_dir=self.config.out_dir,
            config=self.config,
            cache=self.cache,
            budget=self.budget,
            vl=vl,
            rasterer=session if hasattr(session, "render") else None,
            session=session,
            log=self.log,
            doc_dir=self.config.work_dir / "media" / job.doc_id,
            capabilities=capabilities,
        )

    def _vl_allowed(self, spec: Any, state: PageState) -> bool:
        if not self.config.model_enabled or self.vl is None:
            return False
        if Capability.MODEL_CALLS not in self.registry.capabilities_for(spec.plugin):
            return False
        try:
            self.budget.check(state.record.doc_id)
        except BudgetExceeded:
            return False
        return True

    def _can_escalate(self, plan: Plan) -> bool:
        """Whether the ladder may run at all: a route, a client, and live budget.

        Deliberately conservative. A run with no credential, in `--offline` mode,
        or over budget does not fail — it stops escalating and marks the page for
        review, which is a result a person can act on.
        """
        if not plan.verify.escalate_to or not self.config.model_enabled or self.vl is None:
            return False
        return not self.budget.exhausted

    def _escalation_steps(self, plan: Plan, quality: dict[str, Any], attempt: int) -> list[PlanStep]:
        """Build the ladder for one escalation attempt.

        Attempt 1 renders at the standard extraction resolution with thinking
        off. Attempt 2 spends more: higher resolution for anything table-shaped,
        and thinking on, because a page that failed twice is genuinely hard.
        """
        if not plan.verify.escalate_to:
            return []
        tasks = ["ocr", "layout"]
        text = " ".join(quality.get("reasons", []))
        if "table" in text:
            tasks.append("tables")
        if "image" in text or "garbled" in text:
            tasks.append("figures")

        dpi = 150 if attempt == 1 else (200 if "table" in text else 165)
        steps = [PlanStep(op="page_raster", params={"dpi": dpi})]
        steps.append(
            PlanStep(
                op=plan.verify.escalate_to,
                params={"tasks": tasks, "dpi": dpi, "thinking": attempt > 1, "replace": True},
            )
        )
        if attempt > 1:
            steps.append(PlanStep(op="vl_cross_check", params={}))
        return steps

    def _gate(self, job: DocumentJob, plan: Plan, session: Any, state: PageState) -> dict[str, Any]:
        spec = self.registry.ops.get(plan.verify.gate)
        if spec is None:
            return {"gate": plan.verify.gate, "needs_escalation": False, "error": "unknown gate"}
        context = self._context(job, session, state, spec)
        try:
            result = spec.fn(context, apply_defaults(spec.params_schema, {}))
        except Exception as exc:  # noqa: BLE001
            return {"gate": plan.verify.gate, "needs_escalation": False, "error": f"{type(exc).__name__}: {exc}"}
        quality = dict(result.metadata.get("quality") or {})
        state.merge(result, spec.name)
        return quality

    # ── assembly and chunking ───────────────────────────────────────────────

    def _assemble(self, records: list[PageRecord], plan: Plan) -> None:
        """Document-level cleanup that no single page can decide.

        A running head is only recognisable by seeing it on many pages, so this
        is the one pass that must look at the whole document — and it looks at
        counts, not contents.
        """
        if not records:
            return
        occurrences: dict[str, int] = {}
        for record in records:
            seen = set()
            for block in record.blocks:
                text = block.text.strip()
                if not text or len(text) > 120 or text in seen:
                    continue
                seen.add(text)
                occurrences[text] = occurrences.get(text, 0) + 1

        threshold = max(2, int(len(records) * RUNNING_HEAD_FRACTION))
        repeated = {text for text, count in occurrences.items() if count >= threshold}
        if not repeated:
            return

        # Repeated short lines are running heads; they are dropped rather than
        # retyped, because a header on every page is noise in every chunk.
        for record in records:
            kept: list[Block] = []
            for block in record.blocks:
                if block.text.strip() in repeated and block.type in ("paragraph", "heading"):
                    continue
                kept.append(block)
            for index, block in enumerate(kept):
                block.order = index
            record.blocks = kept

    def _chunk_pages(self, job: DocumentJob, plan: Plan, records: list[PageRecord]) -> list[dict[str, Any]]:
        spec = self.registry.ops.get("chunk_for_memory")
        if spec is None:
            return []
        chunks: list[dict[str, Any]] = []
        for record in records:
            if not record.blocks:
                continue
            state = PageState(record=record)
            context = OpContext(
                doc=self._document_record(job),
                state=state,
                work_dir=self.config.work_dir,
                out_dir=self.config.out_dir,
                config=self.config,
                log=self.log,
            )
            try:
                result = spec.fn(context, apply_defaults(spec.params_schema, {"target_chars": 1200}))
            except Exception as exc:  # noqa: BLE001
                record.errors.append(f"chunking failed: {type(exc).__name__}: {exc}")
                continue
            for chunk in result.metadata.get("chunks", []):
                chunk["doc_id"] = job.doc_id
                chunk["kind"] = record.kind
                chunks.append(chunk)
        return chunks

    def _doc_report(
        self,
        job: DocumentJob,
        plan: Plan,
        records: list[PageRecord],
        chunks: list[dict[str, Any]],
        started: float,
    ) -> DocReport:
        route_counts: dict[str, int] = {}
        needs_review: list[str] = []
        gate_failures = 0
        for record in records:
            route_counts[record.route] = route_counts.get(record.route, 0) + 1
            if record.quality.get("needs_escalation"):
                gate_failures += 1
            if record.route == "needs_review":
                needs_review.append(record.page_id)
        errors = [f"{job.doc_id}: {error}" for error in job.errors]
        for record in records:
            errors.extend(f"{record.page_id}: {error}" for error in record.errors[:3])

        return DocReport(
            doc_id=job.doc_id,
            source_path=str(job.path),
            format=job.format_id,
            units=len(records),
            route_counts=route_counts,
            gate_failures=gate_failures,
            escalations=sum(1 for record in records if record.route.startswith("vl")),
            vl_calls=sum(record.vl_calls for record in records),
            prompt_tokens=sum(record.prompt_tokens for record in records),
            completion_tokens=sum(record.completion_tokens for record in records),
            usd_est=round(sum(record.usd_est for record in records), 6),
            pipeline_id=plan.pipeline_id or ("degraded" if plan.plan_degraded else None),
            needs_review=needs_review,
            errors=errors[:50],
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def _serialize_result(result: OpResult) -> dict[str, Any]:
    """Everything a replay needs to reproduce the original effect, not just the
    blocks. A cache entry that replays differently from the call it replaces is
    worse than no cache."""
    return {
        "blocks": [block.model_dump(mode="json") for block in result.blocks],
        "signals": result.signals,
        "replace_blocks": result.replace_blocks,
    }
