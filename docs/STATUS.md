# Status

What is built, what is verified on the run host, what is not, and what the next
three actions are. Written as a handover, not a changelog.

## Verified on the run host (`119.3.235.35`)

The image builds there, and the extraction runtime was exercised over the real
corpus inside it.

| Claim | Evidence |
| --- | --- |
| The image builds and is self-consistent | `docpipe-agent:0.1.0`, 1.18 GB, `selftest` OK |
| The catalog is valid inside the image | `pipelines validate` → `catalog OK: 9 pipeline(s), 19 operator(s)` |
| The corpus extracts, free | 14 documents / 478 units / **0 vision calls / $0.00** |
| Routing is correct per document | 11 PDFs → born-digital, sigmod tutorial → slide-export, lakebase deck → image-slide, Databricks/千问 → text-deck, WorkBuddy → docx-tables |
| The gate finds what needs help | 79 of 478 pages flagged, no others |
| **Vision escalation works end to end** | 4 image-only slides → 4 calls, 4652 prompt / 3320 completion tokens, 69 blocks (18 headings, 21 list items, 2 tables) of correct Chinese content |
| **Escalation is proportional** | 28-page PDF pilot: 25 pages static, **3 escalated — exactly the 3 the baseline had flagged**, 3 calls, then 0 gate failures and 0 review flags |
| **Cache is real, not claimed** | re-running the deck: `vl_calls: 0`, `cache: hits 4`, provenance `op=vl_extract origin=cache` |
| **Cold and warm runs agree exactly** | 28-page PDF pilot: cold → 25 static / 3 escalated / 3 calls / 5161+5521 tokens; warm → *identical routing*, 0 calls, 0 tokens |
| A shippable artifact exists | `/data/mm-lbfs/dist/docpipe-agent-0.1.0.tar.gz`, 471 MB, `sha256 8ad080de…d581b` |
| The image runs as its unprivileged user | every run above used `--user 1000:1000` |
| Tests pass | 23/23 (`pytest`, locally) |
| Plugin layer is installable | pnpm 12.10.1 present, so `dsh plugin --profile … add <pkg>` works |

## PDF extraction performance

The two pdfplumber operators were ~100% of extraction time, and each opened the
document and parsed the page independently. Fixed by caching one pdfplumber handle
per document and returning the same `Page` object to every operator, so the
expensive layout parse runs once and is shared.

| | before | after |
| --- | --- | --- |
| aggregate, 21 pages / 3 documents | 1.98 pages/s | **6.2 pages/s** |
| figure-heavy PDF (Chucky) | 2.7 | 6.0 |
| path-free PDF (Query Optimization) | 2.5 | 21.5 |
| `pdf_tables`, where it runs | 3.51 s | 0.15 s |

`tools/benchmark_pdf.py` exists so this stays measurable. Two results worth
recording as much as the wins:

- A sharper table filter ("skip unless the page has ≥2 wide, thin ruling lines")
  reached 12 pages/s — and **dropped 4 real tables in 32 sampled pages**. An
  equivalence test caught it; the safe rule is the slower one, and the commit says
  so.
- Reading font sizes straight from PDFium (2.4 ms for 6192 characters, versus
  ~250 ms for pdfminer) is 10× faster and gives a clean size histogram (title
  17.2, heading 12, body 9). It is **not** the default: an A/B against pdfminer
  gave 0% agreement, with authors interleaved into affiliations and phantom 140pt
  headings, because line assembly on dense layouts is unfinished. It stays behind
  `backend: "pdfium"` so the work is measurable rather than blind.

## Evaluating correctness

`docpipe evaluate` answers what the gate cannot. The gate asks "did extraction
fail?"; the evaluator asks **"did extraction lose anything?"** — and for a deck
whose text is perfect while its eleven images become eleven empty figure blocks,
those are different answers.

Every unit is re-read from the **source** to establish what was there, and each
object must be accounted for:

| metric | question |
| --- | --- |
| `text_recall` | what share of the source's tokens reached the output (multiset, not diff — reordering is legitimate) |
| `image_accounting` | how many source images were actually *read*, not merely linked |
| `table_survival` | tables the source has versus tables the output carries |
| `clean_pages` | pages with no flag at all |

On the two-document sample: text 100%, images 77.8%, tables 86.1%, **score 0.883**,
8 of 18 pages flagged. The findings are real: three pages with a table the
extractor missed, one garbled page, and an offline run whose image-only slides
produced nothing.

The **agent judge** (`--judge-sample N`) puts the page image and the extracted
content in front of the vision model and asks what is missing or wrong. It is
sampled, budgeted, and prefers pages a metric already flagged. It earned its place
immediately by catching a blind spot in the metric itself: the first version
counted a figure block *pointing at* an image as the image being extracted, and
reported 100% image accounting for a deck whose slides were empty. The judge
reported "提取结果为空" — extraction is empty — on the same pages.

## Measured cost

### Corpus round 6 — what the evaluator caught on real Office documents

Fourteen real-world Office files from Apache POI's test-data (bug-report
attachments, sampled with a stride): 5 pptx, 6 docx, plus a legacy `.doc` and
`.ppt` as negative cases. The evaluator, not a review, found the bugs.

| quantity | before | after |
| --- | --- | --- |
| text recall | 94.1 % | **100.0 %** |
| pages flagged | 13 | **2** |
| score | 0.933 | **0.975** |

Three defects, each general:

- **DOCX recovery read each paragraph twice, or lost it.** The coverage backstop
  keyed "already read" on `id(element)`, and lxml hands out a fresh proxy object
  per access, so a paragraph's identity changes between passes and a freed address
  can be reused by another element. One real Word file scored **9 % text recall**
  while the reader's own text layer held every character. Identity is now the
  element's XPath; the regression test was confirmed to fail against the old code.
- **Repeated text on two pages was deleted as a running head.** The threshold
  collapsed to two on a two-page document, so a two-slide deck's placeholder was
  removed from both slides as typesetter noise. Two pages cannot distinguish a
  header from content that repeats; the floor is three.
- **Expected outcomes were reported as errors.** An offline run that leaves a page
  for review because no vision client exists is behaving as configured. Those
  messages now travel in a separate `notes` channel.

### Wall clock: two costs that dominated large documents

Both were found by running a 357-page Federal Register issue, and both are general.

| per page, same document | before | after |
| --- | --- | --- |
| `page_layout` | 969 ms | 215 ms |
| `text_layer_trust` | 866 ms | 1.3 ms |
| signalling total | 1 865 ms | **244 ms** (7.6×) |
| Federal Register, whole document | 665 s (once it ran at all) | **278 s** |

- **`font_report`'s cache was never written.** The guard read a field nothing
  assigned, so `pdffonts` re-ran on every call — once per page through
  `page_layout`, at 943 ms a time on a document with 1711 fonts.
- **The probe read every page to route on sixty.** Producing a unit means
  profiling it, and each execution window enumerated the whole document before
  filtering to its own range, so a 357-page issue was profiled three times over.
  `enumerate_units` now takes an optional page selection: the probe asks for its
  sample, each window for its range, and a format that ignores the argument stays
  correct and merely slow.

Before these, the Federal Register produced **no extracted page in six minutes**
and had not finished after fourteen; it now completes 357 pages in one pass —
327 static, 26 adapted, 4 left for review, no model calls.

### Corpus round 5 — government documents, and the rule they broke

Seven documents harvested from govinfo and the IRS (12.4 MiB): two daily issues
of the Congressional Record (three-to-four newspaper columns per page), the whole
of US Code title 17, a 300-page Federal Register issue, and three IRS forms.
They were chosen because nothing in the corpus looked like them.

| document | pages | before | after |
| --- | --- | --- | --- |
| Congressional Record 2024-01-09 | 51 | **51 pages refused** | 49 static · 2 review · 34 s · 0 vision calls |
| Congressional Record 2024-01-10 | 80 | **run crashed** | 77 static · 3 review · 72 s · 0 vision calls |
| US Code title 17 | 1 | — | see the form note below |
| Federal Register 2024-01-10 | 300 | 300 pages refused | trusted (measured on samples) |
| IRS f1040 / f1120 / p15 | 2 / 6 / 5 | refused | **still refused — open finding** |

Every Congressional Record page carries ~20,000 characters of *perfect* text
(`118 th CONGRESS, SECOND SESSION ... The House met at 6:30 p.m. and was called to
order`), every one of its 329 fonts unembedded and without a unicode map, so the
trust rule refused all of them. Fixed by making font metadata a *proposal*: the
page's own text decides, by word shape. At ~1,800 + 2,100 tokens per escalated
page, the old rule would have spent roughly **270 K tokens** transcribing the two
Record issues alone, and about **1.2 M** on the Federal Register — to repair text
that was never wrong. See `skills/layout-adaptation/SKILL.md` for the v6 rule.

Estimated, not measured: the Federal Register figure extrapolates from pages that
were sampled, because the full run had not finished when this was written.

### Corpus round 4 — the deterministic pass pays for almost everything

| quantity | value |
| --- | --- |
| corpus | 32 documents / 571 pages (arXiv, ACL, RFC, library fixtures, synthetic scans) + 7 government documents added in round 5 |
| vision calls | **1** — down from 25 pages that wanted vision in round 1 |
| tokens **billed by the pipeline** | 1,990 prompt + 1,402 completion = **3,392** |
| routes | 339 static · **167 adapted** · 62 dynamic · 3 needs_review |
| escalations resolved without a model | **167 units** |
| documents still needing review | 3 (2 pathological Office fixtures, 1 chunker artifact — both since fixed) |

The 167 `adapted` units are the round's result. In round 1 those pages failed
their first plan, were sent to the model, and cost ~1,800 + 2,100 tokens each —
roughly 650 K tokens for the corpus. They are now repaired by a deterministic
operator: a column-aware text pass for multi-column pages, a text-layer-free
path for pages whose text cannot be trusted. The model is reached only when no
deterministic operator can fix the page, which in this corpus happened once.

`adapted` is a new route label, and it exists because the old one lied: every
page that climbed the escalation ladder was reported as `vl_assisted`, including
pages that never touched a model. A run that spent nothing on a page still
reported it as vision-assisted, which hides exactly the saving worth measuring.
A model-produced answer replayed from cache keeps the `vl_assisted` label and is
counted separately in `vl_cache_hits`, because "no spend" and "no model" are
different facts.

### Earlier rounds, for comparison

| quantity | value |
| --- | --- |
| corpus round 1, offline | 484 static · 62 dynamic · 25 flagged · **0 vision calls** |
| per escalated page | **1,806 prompt + 2,096 completion tokens**, `reasoning_tokens: 0` |
| round 2 focused keyed run | 3 documents / 14 pages → 11 calls, 19,861 + 23,062 tokens |
| evaluator (round 2) | score 0.988 · text recall 99.5 % · images 100 % · tables 100 % |
| harness session, all rounds | 1,411 model requests · **2.55 M input + 1.36 M output** tokens · 742 tool calls · ~27 h wall clock over 9 turns |

The largest saving so far came from *not* escalating: an intermediate version of
the text-layer trust rule would have sent 1428 pages of the SQL standard to OCR,
and another 15 pages of a healthy arXiv paper.

## Not verified

- **The agent session in the image runs only with `--security-opt seccomp=unconfined`.** It boots, lists four skills and resolves the vision model, but plain
  `docker run` on this docker 18.09 host fails at `statx` (see *The agent
  blocker* below, which is solved but not portable).
- **The 1428-page SQL standard was not run in the image.** It is the reason the
  PDFium lock exists, and its memory profile is the reason for the streaming gap
  below.
- **No sharded run has been diffed against a single-container run**, so the
  determinism claim remains designed-for rather than demonstrated.
- **The planner's `inspect_page` path and the dynamic (non-catalog) plan path are
  unexercised.** Every document so far matched an archetype; the router never had
  to hand a document to the planner.

## The agent blocker

`dsh --profile headless "<prompt>"` does not start inside the container:

```
Error: dsh: user patch-layer watching requires the Cordis HMR service
    at watchUserPatches (…/dsh-app-boot/lib/index.js:764)
    at runProfile (…/dsh/lib/profile-boot-*.js:264)
```

What was established, so the next person does not repeat it:

- The **same dsh version (0.1.1-rc.2) boots fine on the host**, so this is
  environmental, not a version incompatibility.
- It affects the **shipped `headless` and `web` bundles alike**, and it happens
  **before any preset is mounted** — so the docpipe preset is not implicated.
- It is not a missing package: `healProfilesModuleFallback` populates
  `<DSH_HOME>/profiles/node_modules` with ~197 symlinks including
  `cordis-plugin-hmr`, and the boot fails identically with that fallback fully
  populated and shipped in the image.
- It is not the profile composition: emptying `cordis.patch.yml` changes nothing,
  and adding an explicit HMR row changes nothing.
- `runProfile` guards the auto-load with `ctx.get("hmr") === undefined`, then
  calls `ctx.loader.create({name: '@deepseek-ai/cordis-plugin-hmr'})`. That call
  does not result in a registered `hmr` service. HMR is a file watcher, and the
  container is overlayfs under docker 18.09, so a watch failing to start is the
  likeliest remaining cause.

Two things worth trying next, in order: raise the host's inotify limits
(`fs.inotify.max_user_watches` / `max_user_instances`) and re-run, since that is
the cheapest test of the watcher hypothesis; then run with `DSH_HOME` on a bind
mount rather than the image layer, to separate overlayfs from the plugin.

None of this affects the extraction runtime, which does not need the harness.

## Bugs found by running it, not by reading it

| Bug | How it showed up |
| --- | --- |
| The cache did not preserve `replace_blocks` | a cached vision result *appended* to the garbage it was meant to supersede, so the same document that succeeded cold came back `needs_review` warm — from the cache that exists to make runs identical |
| `SupportsRaster` declared `raster()`, sessions implemented `render()` | **every** raster escalation failed with `object has no attribute 'raster'` — the only route to a vision call, silently degraded to `needs_review` |
| The gate only looked at the text layer | pages the model had just transcribed successfully were re-flagged and sent to review, discarding 69 good blocks |
| Informational diagnostics counted as errors | `report.json` said "2 documents reported errors" on a run where nothing failed |
| Default gate name did not match the registered operator | every degraded plan skipped verification entirely |
| DOCX never reported its unit count | the document extracted **nothing**, silently |
| PPTX geometry published in EMU, not points | a 960 pt slide looked 12 191 999 pt wide and never matched an archetype |
| Unknown signals rejected a routing rule | 11 text PDFs reported "no archetype fits" |
| Layout hints scored like weak signals | the sigmod tutorial was read as a scan instead of a slide deck |
| `None` table cells hit `.strip()` | pdfplumber's normal empty cells raised on every ruled table |
| Markdown headings stripped from unit text | the one line naming a section vanished from output |
| Spaces missing from heading text | `3PROMISE:LSM-TREE&CUCKOO` |
| PDFium used from multiple threads | the process died mid-run with no Python exception |
| `test -w` used to probe writability | warned on every container start about directories that worked — overlayfs makes `access(2)` lie here |
| `daft` declared a hard dependency but used nowhere | 56 MB re-downloaded on every build, and a stalled download cost ~40 minutes |
| `-e DEEPSEEK_API_KEY="$KEY"` inside a single-quoted remote command | expanded on the wrong host; the key never reached the container |
| Every escalated page was labelled `vl_assisted`, including pages a deterministic operator fixed | the report claimed model spend for pages that cost nothing, hiding the saving the escalation ladder produces |
| A DOCX table whose cells exist but whose rows do not | python-docx reports zero rows, the row-driven reader collected nothing, so fifteen characters of text reached no block while the gate reported a table "detected but none extracted" |
| A text chunk holding only the tail newline of a split file | `rfc8446.txt` grew a 57th unit of one character; the gate read it as an unreadable page and sent a healthy document to review |
| The corpus fetcher trusted the HTTP status and the `.pdf` extension | govinfo answers a wrong package id with **200 and an HTML placeholder**, so three document-shaped HTML pages entered the corpus as PDFs; downloads are now checked against magic bytes |
| `--skip` re-runs overwrote `corpus-manifest.jsonl` | harvesting one source discarded the provenance of every other, leaving a manifest that looked complete |
| `Accept-Encoding: gzip` sent to the SEC, which urllib does not decode | EDGAR's JSON arrived as gzip bytes and every filing failed with a decode error |
| `pdffonts` output parsed by token index | its `type` column contains a space (`Type 1C`), so every field to the right shifted by one: `emb` read the encoding and `uni` read the subset flag — on the documents where it mattered most |
| Font metadata treated as a verdict on the text layer | the Congressional Record's 329 unembedded, unmapped fonts extract perfectly; the rule refused all 51 pages, and would have refused a 300-page Federal Register too |
| A multi-line edit moved the window's processing loop into the `TypeError` fallback | every window returned zero outcomes, which reached the report as a document with zero units — the same signature as an empty file. Tests caught it; an invariant now logs `window_produced_nothing` |
| `enumerate_units(pages=…)` narrowing shipped without the callers | the runner's window loop ended up inside the `except TypeError` branch while fixing it, and probes ran against the image's baked copy rather than the working tree |
| `font_report`'s cache guard read a field that was never assigned | `pdffonts` re-ran per page: 943 ms on a 1711-font document, ~5 minutes of a 357-page run |
| The probe profiled every page and then kept a sample | a 357-page issue was profiled three times over (probe + one pass per window) to answer questions about sixty pages |
| DOCX recovery keyed element identity on `id()` | lxml recreates proxies per access, so paragraphs were both duplicated and dropped: 9 % text recall on a real Word file |
| Running-head threshold collapsed to 2 pages | a two-slide deck lost its placeholder text from both slides |
| The sequential window path had no failure isolation | the pooled path caught a failing window and the single-window path did not, so a file that vanished mid-document exited 1 with 40 of 80 pages unread |

## Open findings

- **IRS forms are refused.** `f1040.pdf` (2 units) and `f1120.pdf` (6 units) fail
  the gate on every page while carrying full text. Forms are the one archetype in
  the corpus with no working path: boxed fields, checkboxes and instructions that
  wrap around them. The next round should establish whether the gate is right and
  the content is genuinely not extractable as prose, or the reader is wrong.
- **Parallelism stops at one core on large documents.** PDFium access is
  serialised by a lock and a window maps to a session, so a large document runs
  effectively single-threaded: the Federal Register (357 pages, 6 MB) held
  **99.9 % of one core while using 45 MiB** with `--cpus 12 --memory 24g`, which
  rules out both memory pressure and thread starvation and leaves the lock. The
  probe is also O(pages) before any extraction starts, since it enumerates and
  profiles every page and only then samples. Wall-clock elsewhere is ~0.7-0.9
  s/page (US Code 249 pages in 170 s; Congressional Record 51 pages in 43 s), so
  this is a throughput ceiling rather than a correctness bug, and it is what to
  fix before multi-GB inputs.

## Next three actions

1. **Stream the writers.** `_run_document` holds a whole document in memory before
   writing. That is the blocker for multi-GB inputs and for the 1428-page
   standard, and it is the single highest-value change left.
2. **Exercise the planner.** Point `--plans-from` at a hand-written plan for a
   document no archetype matches, so the dynamic path has coverage rather than
   just a test suite.

## Host operations

`tools/rssh.sh` multiplexes SSH (`ControlMaster`, 10-minute persist), so the ~25
password authentications that preceded the earlier lockout collapse to one per
session. Adding a public key to the host's `authorized_keys` would remove them
entirely and is still the first hardening step worth taking.

Long operations on the host — image builds, corpus runs — should be detached
(`docker run -d`, or `setsid nohup … &`) and polled. A foreground run dies with
the ssh session: that cost one build, one 600-second corpus run, and one stalled
`pip download` before it was fixed.
