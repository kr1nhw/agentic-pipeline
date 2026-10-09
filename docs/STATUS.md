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

## Not verified

- **No agent session has ever started in the image.** See the blocker below. The
  headless profile is broken in this container, and it is the one part of the
  product that has never run.
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

## Next three actions

1. **Fix the container's agent bootstrap** (inotify limits first). Everything else
   about the agent — preset, settings, four skills, pnpm — is in the image and
   verified present.
2. **Stream the writers.** `_run_document` holds a whole document in memory before
   writing. That is the blocker for multi-GB inputs and for the 1428-page
   standard, and it is the single highest-value change left.
3. **Exercise the planner.** Point `--plans-from` at a hand-written plan for a
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
