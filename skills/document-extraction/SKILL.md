---
name: document-extraction
description: Decide between the static pipeline catalog and a dynamic vision-planned pipeline, run an extraction, read the report, and repair what failed. Use for any request that extracts content from PDF, PPTX, DOCX, Markdown, source code or images.
---

# Document extraction

You are driving `docpipe`. Your job is to get trustworthy structured content out of files
at the lowest cost that meets the request. Extraction is not a single pipeline: it is a
choice between a free deterministic path and a paid agentic one, and most of this skill is
about making that choice correctly.

## The two paths

**Static.** `docpipe` scores each input against archetypes in the pipeline catalog
(`docpipe pipelines list`, `docpipe pipelines show <id>`). A confident match runs a fixed,
hand-written operator chain with **zero model calls**. This is the default and the goal:
for a born-digital paper, a text deck, or a Markdown tree it is both cheaper and *more*
accurate than any model, because the text layer is ground truth.

**Dynamic.** The router stays unsure, or the quality gate fails on specific pages, and the
runtime asks the vision model to compose a pipeline from registered operators
(`docpipe plan --doc <doc_id>` writes the plan to `plans/<doc_id>.json`). You may also
author or repair a plan yourself and run it with `--plans-from`.

Choose dynamic for: scans and image-only pages, slide exports with no text layer, pages
where fonts/CMaps produced garbage, tables that pdfplumber cannot see, formulas, figures
that need describing, and genuinely novel formats that no catalog entry covers.

Do **not** choose dynamic for: pages whose text layer is present and sane, pages an
existing archetype already matches, or "just to be safe". Vision is an escalation, not a
default.

## Procedure

1. **Probe before you pay.** `docpipe probe --in <path>` reports format, page count, text
   coverage, ink ratio, embedded images and encoding health per document. Read it.
2. **Try the catalog.** `docpipe run --in /in --out /out --offline` extracts everything
   deterministically. This is the baseline you must beat, and it costs nothing.
3. **Read the report, not the vibes.** `out/report.json` lists each page's route, gate
   result, and cost. Pages that failed the gate are the *only* ones worth spending on.
4. **Escalate narrowly.** `docpipe run --plans-from plans/ --budget …` for the failing
   pages. Inspect a specific page visually with `read_image` on
   `out/rasters/<page_id>.png` when you need to confirm what the gate is complaining about
   — never below 110 dpi, because low-resolution rasters make the model invent content.
5. **Verify.** A deterministic source beats a vision answer every time. When a page has
   *any* text layer, `vl_cross_check` compares the model's reading against it and reports
   the disagreement instead of silently preferring the model.
6. **Report cost.** State pages routed deterministically, pages escalated, and tokens or
   USD spent.

## Writing a plan

Plans are validated, not trusted: unknown operators, bad parameters, a `vl_*` step with no
preceding raster step, or a budget overrun are all rejected, and you get one repair round
before the runtime falls back to the matched static pipeline.

```json
{
  "schema_version": "1",
  "doc_id": "…",
  "default_steps": [
    {"op": "pdf_text_layer", "params": {"layout": true}},
    {"op": "pdf_font_heading_map", "params": {}},
    {"op": "reading_order_v1", "params": {}}
  ],
  "page_overrides": [
    {"pages": [12, 13],
     "reason": "no text layer, ink ratio 0.4, embedded scan",
     "steps": [
       {"op": "pdf_page_raster", "params": {"dpi": 150}},
       {"op": "vl_extract", "params": {"tasks": ["ocr", "tables"], "thinking": false}}
     ]}
  ],
  "verify": {"gate": "default_v1", "escalate_to": "vl_extract", "max_escalations": 1},
  "budget": {"max_vl_calls": 20}
}
```

`docs/plan-schema.md` is the contract; `skills/pipeline-catalog/op-registry.md` is the
generated list of every operator with its parameters.

## Promoting what worked

When a dynamic plan beats the catalog on a class of documents, promote it — that is how
this system gets cheaper over time:

```bash
docpipe promote --plan out/plans/<doc_id>.json --archetype pdf-scanned-image
```

The promoted entry must reproduce the same blocks with `--offline`; if it cannot, it is not
allowed into the catalog. Never promote a pipeline whose quality depends on one model call
that could answer differently tomorrow.

## Failure handling

- `needs_review` pages carry their raster and signals in `out/review/` — look at them
  before changing anything.
- `request_new_op` entries mean the registry has a real gap. Report them; do not work
  around them with an ad-hoc script, which is exactly the drift this architecture exists to
  prevent.
- Budget exhaustion is a clean stop, not a failure: the run is resumable with `--resume`.
