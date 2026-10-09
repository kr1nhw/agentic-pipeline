# Architecture

docpipe turns files into structured content by choosing, per document and per
page, between hand-written operators and a vision model — and by preferring the
operators every time they are sufficient.

The design has one organising idea: **the system knows what it does not know.**
A text layer that exists is ground truth. A table that pdfplumber failed to
extract is a *detectable* failure. A raster below 110 dpi is a known
hallucination risk. Everything else — routing, escalation, budget — follows from
making those facts explicit and cheap to obtain.

## Tiers

```
T0 ingest      detect format from bytes → enumerate units → stream PageRecords
T1 signals     free evidence per page: text layer, geometry, image area, encoding
T2 route       archetype scoring → static pipeline, or admit that none fits
T3 plan        (dynamic only) VL agent composes a plan from the operator registry
T4 execute     plan steps per unit → gate → bounded escalation ladder
T5 assemble    running heads out, reading order, chunks in
T6 materialise pages/blocks/tables parquet · content.md · content.jsonl · report.json
```

### T0 — ingest

Format comes from **bytes, not extension**: an extension is a claim, a magic
number is evidence. Each format plugin opens a per-worker *session* that owns the
file handle and is reused for every unit, because reopening a 500 MB PDF per page
is how a scalable design stops being one.

Enumeration is streaming and fills tier-1 signals as it goes. Nothing is
rasterised here: the decision of whether a page deserves pixels is made from
text, geometry and cheap structure.

### T1/T2 — signals and routing

Units are described by signals that cost nothing: text characters, page geometry,
layout hint, image-coverage fraction, table candidates, encoding health.

The router scores those against the archetype catalog. A confident match runs a
fixed operator chain with **zero model calls**. No match is a first-class
outcome, not a fallback: the document goes to the planner with the near-miss
candidates attached.

Two routing rules were learned the hard way:

- **Unknown is not violated.** A rule that required image coverage disqualified
  every text PDF whose coverage had not been measured yet, which looked exactly
  like "no archetype fits".
- **A layout hint counts double.** Without that, a 16:9 PDF with a thin text
  layer ties between "slide export" and "scanned image", and the alphabetically
  earlier archetype wins — which is how a slide deck gets read as a scan.

### T3 — the agent

The planner receives a *text-only* dossier plus the candidate scores, and may ask
to look at up to `inspect_pages_k` pages before committing. It returns a plan in
the same schema a static pipeline uses, so the executor has one input type.

The operator registry is closed. The agent composes, parameterizes and constrains
registered operators; it cannot invent one. A real gap produces a
`request_new_op` step instead of an improvised script — which is the difference
between a system that accumulates capability and one that accumulates drift.

Every plan is validated before execution: unknown operators, unknown parameters,
out-of-range values, a `vl_*` step with no preceding raster step, and budget
overruns are all rejected. The model gets one repair round with the exact
validator message, then the runtime falls back to the matched static pipeline and
marks the plan `degraded`.

### T4 — gate and escalation

The gate is the cost lever of the whole system, so it must fail loudly and
narrowly. It flags: garbled encoding (CID placeholders, replacement characters),
an image-dominated page with no text, a page with content but no blocks, table
candidates with no extracted table, and mostly-empty tables.

Only a gate failure reaches the ladder, and the ladder is bounded
(`max_escalations`, default 1 per stage):

```
static pipeline → +layout operators → vl_extract(thinking off, 150 dpi)
     → vl_extract(thinking on, higher dpi) → needs_review
```

Every vision answer is checked against the text layer where one exists
(`vl_cross_check`). **The text layer is authoritative.** A vision reading that
contradicts it is reported, not preferred — replacing ground truth with a
plausible-looking reading is the most damaging failure this system could have.

### T5/T6 — assembly and output

Assembly is the one pass that must see the whole document: a running head is only
recognisable by appearing on most pages. It looks at counts, not contents.

The canonical IR is `pages.parquet` + `blocks.parquet`. Markdown, JSONL and the
memory chunks are derived views — anything not reproducible from the IR is a bug.

## Work model and scale

Work is fanned out as **page windows**, each opening its own session. That one
choice gives parallelism within a single huge document and across a corpus, keeps
per-worker memory bounded to a window, and means a killed worker loses a window
rather than a document.

- Rasters go to disk with an LRU budget; they are the usual cause of a runaway
  container.
- Per-document Markdown and JSONL are written as documents complete, so they are
  readable mid-run. The parquet artifacts are **not**: row groups flush as pages
  complete, but a parquet footer is only written when the writer closes, so a
  parquet file from a killed run is not readable until it is re-run to
  completion. Treat the JSONL as the mid-run view and the parquet as the
  product.
- Every operator result is cached against `(page content, op, params, model)`.
  A re-run of a completed corpus makes zero model calls and rewrites identical
  artifacts.
- `--shard i/N` partitions by content hash, so a shard's membership does not
  depend on which other files happen to be present.
- Budgets are hard stops: exhaustion is a clean, resumable stop, not a
  degradation to a worse pipeline.

## The plugin standard

Two layers, with the Python layer as the single source of truth for what an
operator is:

| Layer | Extends | Installed with |
| --- | --- | --- |
| docpipe plugin | formats, operators, pipelines, exporters, hooks | entry point `docpipe.plugins`, `$DOCPIPE_PLUGIN_PATH`, `/plugins`, `--plugin` |
| DSH plugin | the agent: tools, services, prompt sections | `dsh plugin --profile docpipe add <pkg>` |

Builtins register through the same `PluginAPI` a third-party plugin receives. If a
builtin needed a privileged path, the plugin interface would be second class and
the "extend it the way we do" promise would be false.

Capabilities are **enforced where enforcement is real**: a `vl`-class operator
cannot be registered without `model_calls`, the executor hands `ctx.vl` only to
operators whose plugin declared it, and `ctx.run` raises without `subprocess`.
Filesystem writes are *declared, not jailed* — a plugin runs in-process with the
runtime's privileges — and the spec says so rather than implying a sandbox.
See `docs/plugin-spec.md`.

## Known gaps

Recorded because they are real, bounded, and have a specific next step.

1. **Native crashes are not isolated.** PDFium is not thread-safe, and the
   symptom is the whole process dying rather than a Python exception. Every
   PDFium call is now serialised behind a lock, which fixed a reproducible crash
   on the 1428-page SQL standard at `--workers 8`. The robust fix is
   process-per-window isolation so a corrupt page kills a worker, not the run.

   The same run also exposed the next scale limit: `_run_document` collects every
   unit of a document in memory before writing it out, so a document's peak
   footprint is its whole page set, not one window. Streaming each window's rows
   straight into the writers is the fix, and it is required before a multi-GB
   single file is attempted.

2. **Multi-column pages are bought, not computed.** `page_layout` reports a column
   count, and a page with two columns is escalated to the vision path, which reads
   them correctly because it sees the page. Making it deterministic was attempted
   and reverted: three criteria for locating the gutter each failed on a real ACL
   page whose columns are visually obvious. The next criterion to try is minimum
   line crossings; `skills/layout-adaptation/SKILL.md` records all four. Measured
   cost of the procurement route: **1,806 prompt + 2,096 completion tokens per
   escalated page**, with `thinking` disabled (`reasoning_tokens: 0`).

3. **Two-column PDFs are read in layout order.** `pdftotext -layout` preserves
   lines but not columns, so a two-column paper's left and right columns are
   interleaved within a line. The characters are the file's own and each column's
   text is intact; only the interleaving is wrong. A character-box path exists
   behind `pdf_text_layer(geometry=true)` but is off by default: a band of
   equal-y characters can contain both a table caption and an adjacent body line
   whose x-ranges overlap, and sorting that band by x interleaves the two. Doing
   it properly needs real layout analysis (pdfminer's `LTTextLine` objects) or
   explicit column-region operators.

3. **Office rendering is structural, not visual.** PPTX and DOCX are read through
   python-pptx and python-docx, which is exact for text, tables and pictures, and
   leaves the vision path reachable only when a slide is a single full-bleed
   picture. There is no synthetic slide renderer, deliberately: approximating a
   layout and then asking a model to read the approximation adds a failure mode
   without adding information.

4. **Audio and video are extension points.** The format plugin contract has byte
   range units and a `max_unit_bytes` declaration for them; no operator implements
   them yet, and the image does not ship ffmpeg by default.

5. **Cost is reported, not predicted.** Token accounting is exact; the price table
   defaults to zero, so `usd_est` stays 0 unless configured. An invented cost is
   worse than an absent one.
