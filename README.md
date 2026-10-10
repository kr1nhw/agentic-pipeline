# docpipe — an agent image for document content extraction

`docpipe-agent` turns files (PDF, PPTX, DOCX, Markdown, source code, images) into
structured content — text, headings, tables, figures, formulas, code — with provenance
on every block.

It is *agentic* in a specific, bounded sense: hand-written operators handle the formats we
know, a **static pipeline catalog** runs them for free, and a **vision-language agent**
composes a pipeline out of those operators only when the deterministic router is unsure or
a page fails the quality gate. The operator registry is closed — the model selects and
parameterizes, it never invents.

## What it costs

Cheap and deterministic first, a model last, and every rung has to be able to say it
cannot help. Measured on a 1 374-page corpus with vision enabled and a cold cache:

| quantity | value |
| --- | --- |
| vision calls | **19** — 1.4 % of the pages |
| tokens | **56.8 per page** (4 105 per escalated page) |
| pages repaired by the deterministic rungs | **410**, against 19 that needed a model |
| counterfactual without those rungs | ~1.76 M tokens — **22.5× more** |

Local OCR (tesseract) is a rung, not a fallback: a page that is an image has no
characters for `pdftotext` and no fonts for the trust rule, so without it every such
page costs a model call. With it, an image-only PDF of a 1789 scan reads at 1.000 for
zero tokens, and the model is kept for pages OCR cannot manage.

Measured end to end, offline: **1.000** on the main corpus (1 374 pages), **0.998** on a
102-document harvest (3 207 pages), **0.973** on thirty PDFs contributed to Apache Tika
because they broke it.

## Why an image

The product is the image, not a host install. It carries the extraction runtime, the
DeepSeek Harness agent, the pipeline catalog, the skills that teach the agent when to use
which path, and the plugin loader — so a user gets a working, extensible extraction agent
with one `docker run`, and can add capability with `dsh plugin add` or a format plugin
without rebuilding anything.

## Quick start

```bash
# judge what a run kept: coverage, not vibes
docker run --rm -v /path/to/out:/out docpipe-agent:0.1.0 \
  docpipe evaluate --out /out --judge-sample 5

# build (see docs/operations.md; the build host needs docker + network)
tools/build_image.sh

# batch extraction
docker run --rm \
  -v /path/to/documents:/in:ro \
  -v /path/to/output:/out \
  -e DEEPSEEK_API_KEY=sk-… \
  docpipe-agent:0.1.0 docpipe run --in /in --out /out

# the agent itself (vision-capable, docpipe preset, four skills loaded)
docker run --rm -it \
  -v /path/to/documents:/in:ro -v /path/to/output:/out \
  -e DEEPSEEK_API_KEY=sk-… \
  docpipe-agent:0.1.0 dsh
```

No key? Everything deterministic still works:

```bash
docker run --rm -v "$PWD/dataset:/in:ro" -v "$PWD/out:/out" \
  docpipe-agent:0.1.0 docpipe run --in /in --out /out --offline
```

## What it writes

| Artifact | Contents |
| --- | --- |
| `pages.parquet` | one row per page/slide/section: signals, route, plan, cost, provenance |
| `blocks.parquet` | the extraction IR: typed blocks with text, table payloads, bboxes, source op |
| `tables.parquet` | every extracted table as rows, with its page and bounding box |
| `docs/<doc_id>/content.md` | reading-order Markdown with page markers |
| `docs/<doc_id>/content.jsonl` | one block per line, with provenance |
| `chunks.jsonl` | memory-oriented chunks: text + heading path + page/bbox pointers |
| `report.json` | per-document status, gate failures, escalations, token and cost totals |
| `evaluation.json` | what the source contained, what the output kept, and what was lost |
| `review/` | `needs_review` evidence and `request_new_op` requests |

## Repository map

```
Dockerfile              the product image (classic-builder compatible, no BuildKit)
docker/                 entrypoint + healthcheck
docpipe/                the runtime: schema, ingest, ops, router, agent, exec, store
docpipe/api/            ★ plugin API v1 — the extension standard
pipelines/              static pipeline catalog (declarative YAML)
presets/docpipe/        the harness agent preset baked into the image
skills/                 the four skills the agent loads
examples/               reference plugins (Python format plugin, DSH plugin)
docs/                   architecture, plan schema, plugin spec, operations runbook
tools/                  rssh, build, run, sync, in-image test helpers
```

## Documentation

- `docs/architecture.md` — tiers, data flow, why the agent sits where it does
- `docs/plan-schema.md` — the plan the agent emits and how it is validated
- `docs/plugin-spec.md` — plugin API v1: formats, ops, pipelines, capabilities
- `docs/operations.md` — building, running, sharding, budgets, troubleshooting
- `docs/op-library.md` — generated from the registry (`docpipe ops --markdown`)
