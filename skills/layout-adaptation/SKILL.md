---
name: layout-adaptation
description: Decide per PAGE how to extract, when a document's layouts vary — multi-column papers, scanned or OCR'd pages, slide exports, table pages, formula-heavy pages. Load before extracting anything whose layout is not uniform, and whenever a document mixes born-digital text with scans.
---

# Page-level adaptation

A document is not one layout. A scanned appendix, a two-column insert, a
landscape table page and a slide export are all normal inside one file, and a
single document-level decision cannot express any of them. This skill is about
deciding **per page**, from evidence, without adding a special case for every
paper you meet.

## The rule that keeps this from becoming hardcoding

**Route on measured signals, never on document identity or filename.** No
`if "sigmod" in name`, no `if pages > 100`. Every decision below comes from a
number the runtime measured on the page in front of it. When a new layout
appears, it appears as a *new combination of signals* — which the existing
routes already cover — or as genuine new evidence, which is when you add a
signal, not a branch.

## Start here: ask the runtime what each page needs

```bash
docpipe probe --in /in --pages
```

```
   page   chars  cols  trust  imgs  rules  needs
      1    2447     1   0.15     1      0  ocr
      2    9279     2   0.591    0      9  layout,tables
      3    9502     1   0.591    0      2  tables
```

| column | what it measures | what it tells you |
| --- | --- | --- |
| `chars` | characters in the text layer | whether there is a layer at all |
| `cols` | text columns, measured from a real gutter in character coverage | a multi-column layout |
| `trust` | how much the text layer can be believed, 0–1 | whether it is an **OCR layer** |
| `imgs` | images the source page contains | whether content is in pixels |
| `rules` | wide, thin vector paths (ruling lines) | whether a table is drawn |
| `needs` | the routes the signals imply | where to spend effort |

`needs` is a summary, not a verdict. You are the one who decides, and you may
disagree — but you should disagree with a reason the evidence supports.

## The archetypes, and what each actually needs

| Evidence | Page kind | Route | Why |
| --- | --- | --- | --- |
| `trust ≥ 0.4`, `cols = 1` | born-digital prose | `pdf_text_layer` → `pdf_font_heading_map` → `reading_order_v1` | the layer is exact; a model can only make it worse |
| `trust < 0.4` | OCR layer over a scan | `page_raster` → `vl_extract(tasks=[ocr,layout,formulas])` | the characters are *inferred from a font name*, not read. See below |
| `cols ≥ 2` | multi-column | `page_raster` → `vl_extract(tasks=[layout,ocr])` | a flat layer preserves lines but not columns; the columns interleave |
| `rules ≥ 2` | ruled table page | add `pdf_tables(strategy=lines)` | the rules are there to be used |
| `imgs ≥ 1` and few chars | image or scan page | `page_raster` → `vl_extract(tasks=[describe])` | the content is pixels |
| aspect ≈ 16:9, `chars` low | slide export | `vl_extract(tasks=[layout])` | drawn tables, shape layout, no reading-order signal |
| formulas, few words | formula-heavy | `vl_extract(tasks=[formulas])` on the region | symbol identity is lost without a unicode map |

## Why `trust` exists, and why it is the signal people miss

A born-digital PDF embeds subset fonts with a Unicode map: extraction is a
dictionary lookup, and the characters are exactly the author's. An OCR'd scan
references a system font by name with no map, so extraction *guesses* the
character from the glyph and the font name.

That guess is fluent and wrong. A 1950s topology paper reports 2447 characters a
page, 195 words, nine replacement characters — every other check passes it — and
its text layer renders:

| the page says | the layer says |
| --- | --- |
| `R ⊂ X × Y` | `R C X x Y` |
| `R ∘ S` | `R o S` |
| `τ(R)` | `~'(R)` |
| `R[1] ∪ R[2] ∪ ⋯` | `R tl] U R ~1 U R [31` |
| `for n ≥ 2` | `for n i> 2` |

Nothing in the text signals a problem. The fonts do. **v5: `trust` is low when no
font in the document carries a Unicode map** — `pdffonts` two ways:

```
pdffonts scan.pdf   -> TimesNewRoman  TrueType  no no no      # no map anywhere
pdffonts paper.pdf  -> RKVWCA+LinLibertineTI  Type 1  yes yes yes
```

But font metadata is a **proposal, not a verdict**, and the corpus round of 2024
proved it in both directions on documents whose text was checked by eye:

| document | fonts | text |
| --- | --- | --- |
| Congressional Record | 329, **none** embedded, **none** with a map, 86 % `WinAnsi` | **perfect** — `118 th CONGRESS, SECOND SESSION … The House met at 6:30 p.m.` |
| 1982 ACM survey | 12, none embedded, none with a map | **correct**, rare substituted letter (`GONTENTS`) |
| Federal Register | 1711, none embedded, none with a map, 82 % `WinAnsi` | **correct** |
| 1950s topology paper | system font, no map | **garbled** — the table above |

A font with a *standard* encoding (`WinAnsi`, `MacRoman`, `Standard`) has a
byte-to-character table that needs no CMap at all. So v5's rule alone would send
**51 pages of the Congressional Record and every page of a 300-page Federal
Register to vision** — hundreds of thousands of tokens to fix text that was never
broken.

**v6, current: the metadata proposes and the text decides.** Fonts without maps
raise the question; the page's own text answers it, by word shape:

```
trust = 0.2  only if  (no unicode maps)  AND  (word shape < 0.60)
```

Word shape is the fraction of whitespace-separated tokens made of letters. Prose
is built of words; a layer decoded from glyph names is built of `~'(R)`, `R tl]`,
`U{Ry`. Measured on the four documents the font signal cannot clear:

| document | word shape | verdict |
| --- | --- | --- |
| topology paper | **0.47** | refuse — text unusable |
| Federal Register | 0.77 | trust |
| 1982 survey | 0.92 | trust |
| Congressional Record | 0.93 | trust |

It is deliberately language-neutral — it asks for the shape of a word, not for a
dictionary entry — and a page with fewer than twenty tokens is not convicted on
this evidence at all.

Version history, because every earlier version was wrong on a real document:

| version | rule | what it broke |
| --- | --- | --- |
| v1 | unembedded **and** unmapped | borderline documents flickered |
| v2 | graded by the Unicode ratio | flagged the 1428-page SQL standard, whose 15-of-16 **embedded** fonts extract perfectly |
| v3 | graded by embeddedness | flagged Chucky and the sigmod tutorial — born-digital, many unembedded maths fonts carrying little text |
| v4 | both ratios ≥ 60% | still flagged an arXiv statistics paper sitting at exactly 33/55 unmapped |
| v5 | no font maps → distrust | correct on seven calibration documents, and wrong on the whole GPO family (below) |
| **v6** | **no maps AND word shape < 0.60** | correct on every document measured so far |

Embedding is deliberately *not* part of the rule. It looks like the obvious
discriminator and it is wrong in both directions: an embedded font can lack a map,
and an unembedded one can carry the text correctly.

Two parsing traps sit under all of this, and both were live in the shipped code:

1. `pdffonts` inserts an `encoding` column and a `prob` column depending on the
   document, so fixed column indices read `Builtin` as `emb`.
2. **The `type` column contains a space** (`Type 1C`, `Type 0`). Splitting a row on
   whitespace shifts every field to its right by one — so `emb` reads the encoding
   and `uni` reads the subset flag. Parsing by *header position* does not fix this
   either, because the header has no such space; only **character offsets** do.

**Parse that table by character offsets from the header, never by token index.**

When `trust` is low *and the text confirms it*, **do not repair the layer —
replace its source.** Send the
page image and ask for a transcription. On the topology paper that recovers
`R ⊂ X × Y`, `τ(R)`, `R[n−1] for n ≥ 2` and every symbol, and the model corrects
`~'(R)` back to `τ(R)` because it understands the mathematics rather than the
glyphs.

## Multi-column pages: what is measured, and what is not

`probe --pages` reports `cols`, and it is the number of columns the runtime
believes it can see. Treat it as **a routing signal, not a solution**: today a
page with `cols >= 2` is escalated to the vision path, which reads the columns
correctly because it sees the page.

Making that deterministic — separating the columns from line geometry, with no
model call — was attempted and is *not* finished. Three criteria were tried, each
against a real harvested ACL page whose columns are visually obvious
(x = 71..290 and x = 306..526, with a 16 pt gutter between them):

| criterion | why it failed on that page |
| --- | --- |
| a band no character occupies | one full-width caption crosses the gutter and fills it |
| the largest gap in line *starts* | indented lines inside a column put the boundary mid-column, collapsing the page to one column |
| the band with the *sparsest* occupancy | a handful of lines end past the column edge, leaving the gutter 80 % occupied |

The criterion that should work next is **minimum line crossings**: a boundary
inside a column is crossed by every line of that column, while a boundary in the
gutter is crossed only by the occasional overlong line, so the minimum is sharp
rather than marginal. That was implemented and then reverted unverified, because
each iteration cost more than the tokens it would save and the escalated route
already produces correct output.

**Do not describe multi-column extraction as solved.** It is currently bought,
not computed.

## Composing a per-page plan

A document that is *mostly* one layout states the default once and overrides the
pages that differ. This is the whole mechanism — it is a plan, not code:

```json
{
  "schema_version": "1",
  "doc_id": "…",
  "default_steps": [
    {"op": "pdf_text_layer", "params": {}},
    {"op": "pdf_font_heading_map", "params": {}},
    {"op": "reading_order_v1", "params": {}}
  ],
  "page_overrides": [
    {"pages": [[40, 44]],
     "reason": "probe: trust 0.15, 1 image, 0 chars of usable text — OCR layer",
     "steps": [
       {"op": "page_raster", "params": {"dpi": 200}},
       {"op": "vl_extract", "params": {"tasks": ["ocr", "layout", "formulas"], "thinking": false}}
     ]},
    {"pages": [2, 6, 13, 14],
     "reason": "probe: cols=2, gutter measured at x≈300 — flat layer interleaves columns",
     "steps": [
       {"op": "page_raster", "params": {"dpi": 165}},
       {"op": "vl_extract", "params": {"tasks": ["layout", "ocr"], "thinking": true}}
     ]}
  ]
}
```

Write the `reason` from the probe output. A plan whose reasons do not match the
evidence is how a pipeline drifts.

## Structure may be downgraded; content may not be dropped

A table that fails to qualify as a table is still content. On a harvested Word
fixture the text layer carried `T2` and `T4` while the emitted blocks carried
nothing, because a 2x2 table has one non-empty row and failed the two-row test.
PDFs hid the bug — their text layer re-emits table characters as paragraphs — but
a DOCX emits paragraphs and tables from separate operators, so a rejected table
had no fallback at all.

Wherever an operator decides "this is not a table / not a heading / not a list",
the content still has to go somewhere. Downgrade the structure, never the content.

## Cost discipline

Two costs matter and only one of them is tokens. Wall-clock is the other, and a
single page can dominate a corpus: one harvested ACL paper carried a scatter plot
drawn as **73,202 vector objects**, which pdfminer decomposes into one line object
each, and `find_tables` spent **3.17 s** on it against ≤0.07 s on every other page.
Sixteen pages of that document cost 149 s — 3% of a 32-document corpus's entire
runtime. Table detection is therefore skipped above `max_vector_objects` (default
20 000): past that count a page is a figure, not a table grid, and the guard pays
for itself many times over. Note PDFium reports only 118 page objects for that
same page, so the explosion is invisible until pdfminer has parsed it.

Escalation is per page and bounded, and the gate already fires only where the
deterministic path provably failed. Two more rules:

- **Batch by archetype, not by page.** If twelve pages share `trust=0.15`, they
  are one decision, not twelve.
- **Check the cache before spending.** Vision results are cached against page
  content, operator and parameters, so re-running a corpus costs nothing. If a
  re-run reports vision calls on unchanged input, something is wrong — report it.

## When the evidence is new

If a page's signals match no archetype — a form, a musical score, a handwritten
letter, a page whose `cols` estimate is 3 — that is **new evidence, and the
answer is a new signal or a new operator, not a special case in a branch**:

1. Say what you observed and which signal failed to describe it.
2. Emit a `request_new_op` step with the page id and the evidence.
3. Extract that page through the closest existing route so the run still
   completes, and mark it in the report.

Then the next person adds a *measurable* signal, and every document benefits.
That is the difference between a system that gets better at documents and one
that accumulates exceptions.
