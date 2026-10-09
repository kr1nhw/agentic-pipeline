---
name: pipeline-catalog
description: The shipped document archetypes, how routing decides between them, and how to add an archetype. Load before extracting a large corpus, and whenever a document type keeps needing the same dynamic plan.
---

# Pipeline catalog

Every entry is a hand-written, deterministic operator chain for a document type we already
understand. They are the reason most extractions cost nothing.

```bash
docpipe pipelines list            # id, match rule, step count, version hash
docpipe pipelines show <id>       # the full YAML
docpipe pipelines validate        # schema-check the whole catalog
```

The operator vocabulary is generated from the live registry — never hand-edited:

```bash
docpipe ops --markdown > skills/pipeline-catalog/op-registry.md
```

## Shipped archetypes

| id | matches | shape |
| --- | --- | --- |
| `pdf-born-digital-text` | pdf, text coverage ≥ 0.85, images < 20 % | text layer → font-size headings → line-strategy tables → reading order |
| `pdf-scanned-image` | pdf, text coverage < 0.15, ink high | raster 150 dpi → `vl_extract(ocr, tables)` → cross-check if any text exists |
| `pdf-slide-export` | pdf, page box ≈ 16:9 | text layer → shape-ish blocks by bbox → figure extraction |
| `pdf-formula-heavy` | pdf, math glyph density high | text layer → formula regions → `vl_extract(formulas)` on those regions only |
| `pptx-text-deck` | pptx, most slides carry text runs | python-pptx shapes in z-order → tables → notes |
| `pptx-image-slide` | pptx, slide has one full-bleed picture | extract the picture → `vl_extract(describe, tables)` on it |
| `pptx-diagram-deck` | pptx, graphicFrame/SmartArt present | shape text → diagram XML text → figures as placeholders |
| `docx-report-tables` | docx, ≥ 3 tables | styled paragraphs → tables → footnotes |
| `md-notes` | md/txt | read → ATX section split → code fence extraction |
| `code-repo` | source extensions | read → language detect → symbol outline → comment extraction |
| `image-single` | png/jpg/webp | probe stats → `vl_extract(describe)` when asked |

## Routing

The router scores cheap signals only — format, text coverage, ink ratio, image area
fraction, font histogram, aspect ratio, table-candidate count. A confident winner runs
immediately. Below the confidence floor the page goes to the planner, and the dossier it
receives includes the top-three candidates, so the model usually confirms a near-miss
rather than inventing a pipeline from scratch.

A page can therefore be routed statically *within* a dynamically-planned document and vice
versa: routing is per page, not per file.

## Adding an archetype

1. Extract the document type with `--offline` and read `report.json`: which pages failed,
   and which operator fixed them when you escalated by hand.
2. Write the YAML with a `match` block that is *narrow enough to be safe* — an over-broad
   archetype silently degrades every document that matches it.
3. `docpipe pipelines validate`, then re-run a representative corpus with the new entry and
   diff `blocks.parquet` against the previous run. The catalog entry earns its place only if
   it matches or beats the old route.
4. Prefer promoting a proven dynamic plan: `docpipe promote --plan … --archetype <id>`.

Rules:

- The catalog is versioned and content-hashed; every artifact records the `pipeline_id` and
  hash that produced it. Never edit a shipped entry in place — add a version and let the
  router choose.
- An archetype must be reproducible with `--offline`. If it cannot run without a model
  call, it is a *template for the planner*, not a catalog entry.
