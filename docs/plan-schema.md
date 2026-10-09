# Plan schema

A **plan** is what the runtime executes for one document. Static catalog entries
and dynamic agent output lower to exactly this type, so the executor has one
input and a promoted plan is indistinguishable from a hand-written one.

```jsonc
{
  "schema_version": "1",
  "doc_id": "55faa3e3d8587040",
  "pipeline_id": "pdf-born-digital-text",   // set for a catalog entry, null for agent output
  "pipeline_hash": "…",                     // what actually produced these artifacts

  "scope": { "pages": "all" },

  "default_steps": [
    { "op": "pdf_text_layer",       "params": { "preserve_lines": false } },
    { "op": "pdf_font_heading_map", "params": { "size_ratio": 1.12 } },
    { "op": "pdf_tables",           "params": { "strategy": "lines" } },
    { "op": "reading_order_v1",     "params": {} }
  ],

  "page_overrides": [
    {
      "pages": [12, 13, [40, 45]],
      "reason": "no text layer, ink ratio 0.4 — embedded scan",
      "replace": true,
      "steps": [
        { "op": "page_raster", "params": { "dpi": 150 } },
        { "op": "vl_extract",  "params": { "tasks": ["ocr", "tables"], "thinking": false } },
        { "op": "vl_cross_check", "params": {} }
      ]
    }
  ],

  "assembly": { "order": "reading_order_v1", "drop": ["header_footer"] },
  "verify":   { "gate": "gate_default_v1", "escalate_to": "vl_extract", "max_escalations": 1,
                "cross_check_sample": 0.05 },
  "budget":   { "max_vl_calls": 40 },
  "notes": "matched pdf-born-digital-text at 0.80",
  "plan_degraded": false
}
```

## Page selectors

`pages` accepts an integer, a `[start, end]` range, or a list of both:
`[12, 13, [40, 45]]` means pages 12, 13 and 40–45. A reversed range is read as
the range it plainly means rather than rejected. Overrides are checked in order,
and the first one naming a page wins.

## Validation

Validation happens **before execution**, and the model gets exactly one repair
round with the validator's own message. It rejects:

| Rejected | Why it matters |
| --- | --- |
| unknown operator | the plan would silently do less than it claims |
| unknown or out-of-range parameter | a dropped parameter is a pipeline that appears to work |
| a `vl_*` step with no preceding raster step | the model would be asked to read a page that was never rendered |
| an unknown gate or escalation operator | verification would be skipped while looking present |
| a page selector naming nothing | usually a typo, and the page would silently keep the default path |
| a budget the run cannot honour | better to refuse than to stop halfway through |

After the repair round the runtime falls back to the matched static pipeline and
sets `plan_degraded: true`, which is visible in `report.json` — a degraded plan
is never silent.

## Budgets

Budgets are hard stops, checked per page and per document. `max_vl_calls`,
`max_tokens` and `max_usd` in the plan narrow the run-level budget for that
document; they can never raise it. Exhaustion produces a clean, resumable stop.

## Provenance

Every executed step appends an entry to the page's `provenance`:

```json
{ "op": "vl_extract", "params": { "dpi": 150, "tasks": ["ocr"] },
  "cost": { "vl_calls": 1, "prompt_tokens": 807, "completion_tokens": 739,
            "reasoning_tokens": 672, "usd": 0.0, "duration_ms": 2400 },
  "ok": true, "origin": "run" }
```

`origin` is `run`, `cache` or `escalation`, which is how a re-run can be shown to
have made no model calls rather than merely claimed to.

## Promoting a plan into the catalog

A dynamic plan that beats the catalog on a class of documents is the raw material
for a new archetype:

```bash
docpipe promote --plan out/plans/<doc_id>.json --archetype pdf-scanned-image
```

The promoted entry must reproduce the same blocks under `--offline`. If it cannot,
it is not a catalog entry — it is a template for the planner, and the distinction
is enforced by that check rather than by intent.
