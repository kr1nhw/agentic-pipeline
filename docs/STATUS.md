# Status

## Where this stands

A document-extraction agent that decides per page how to read it, spends a model call
only when no deterministic operator can do the job, and measures itself continuously.
The numbers below are all from runs on the run host, not estimates.

| corpus | documents / pages | score | model tokens |
| --- | --- | --- | --- |
| main corpus (papers, government, Office, forms, scans) | 39 / 1 374 | **1.000** | **0** |
| fresh harvest (24 two-column, 16 table-heavy, 8 formula-heavy, decks, Word, statutes) | 102 / 3 207 | **0.998** | **0** |
| adversarial (PDFs contributed to Apache Tika because they broke it) | 30 / 41 | **0.973** | **0** |
| image-only PDF from a 1789 scan | 1 / 5 | **1.000** | **0** |

With vision enabled on the main corpus (cold cache, 30-call cap):

| quantity | value |
| --- | --- |
| vision calls | **19** — 1.4 % of pages |
| tokens | 32 775 prompt + 45 220 completion = **77 995** |
| per page | **0.014 calls · 56.8 tokens** |
| pages repaired deterministically instead | **410** — 22× the number that reached the model |
| counterfactual without the ladder | ~1.76 M tokens, 1 282 per page |
| **reduction** | **22.5×** |

Wall clock: ~1 s per page for extraction, 244 ms per page of signalling (7.6× faster
than at the start of this work), 0.6 s per page to evaluate.

## How the numbers got here

Eleven rounds of harvesting documents, running them, and folding what broke back into
general rules. Every round's finding was a rule, never a document:

| finding | what it changed |
| --- | --- |
| `pdffonts` parsed by token index, and its `type` column contains a space | parse by character offsets from the header |
| Font metadata treated as a verdict on the text layer | the text decides: word shape, with the fonts as the precondition |
| Word shape applied without that precondition | mathematics was reported as debris on 166 pages |
| `replace_blocks` cleared every block | the column escalation discarded the tables the plan had already extracted |
| An escalated page labelled `vl_assisted` with no model call | cost reporting that hid its own saving |
| A running head needed only two pages of evidence | a two-slide deck lost its placeholder text |
| A DOCX backstop keyed element identity on `id()` | text duplicated and dropped; 9 % recall on a real file |
| Text removed as a "duplicate" reported as lost | 98 % of claimed loss was repetition, 11 tokens were real |
| An image extracted but unread reported as missing | linked is not lost |
| One PDFium call outside the lock | a 357-page document segfaulted the run, three times |
| The probe profiled every page to route on sixty | page-selective enumeration |
| `font_report`'s cache was never written | `pdffonts` re-ran per page: 5 minutes of a 357-page run |
| A legitimate outcome recorded as an error, four times | notes versus errors, again and again |
| A contract gap: a key one stage wrote, another read by a different name | the gate publishes `content_chars`; the evaluator loads `provenance` |

## Known gaps, honestly

- **A defective OCR layer cannot be detected locally.** The 1789 volume's layer reads
  `Ses s . I. Ch . 24.` and passes every check: its fonts carry a Unicode map, recall is
  100 % because it matches itself, and word shape is 0.91–0.96. Three detectors were
  tried and all three failed — single-letter rate, and agreement with a fresh OCR,
  which agrees with the defective layer at 0.97 while disagreeing with clean born-digital
  documents at 0.21–0.31. Detection needs a dictionary, a model, or a person.
- **Images are published, not read.** A figure is extracted with its bytes and linked;
  reading it is the model's job and is priced accordingly.
- **Forms sit at 50–100 % table survival.** A form is a grid whose cells are mostly
  empty by design, and the acceptance rule rejects mostly-empty tables.
- **Multi-GB inputs are untested.** Documents are held in memory before writing.
- **No sharded run has been diffed against a single-container run.**

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

### Corpus round 14 — a page's figures are published, and three regressions caught by the measure

Offline, a PDF page's images were nowhere in the output: the vision path renders the
whole page for a model to describe, and nothing else touched them. On thirty
adversarial PDFs that was the entire residual — eight pages of unaccounted images on a
page whose meaning is its charts.

`pdf_page_images` now extracts the images a page embeds, one figure block each with
its box and its bytes on disk, and skips pages that have none for free using the count
the page profile already computed. It runs in the born-digital and slide-export plans
and in the format's fallback operators, before `caption_blocks` so captions have
figures to attach to. What it produces is **linked, not read**: a URI and no text.

| adversarial corpus, 30 documents | start of round | now |
| --- | --- | --- |
| pages flagged | 13 | **1** |
| documents reporting errors | 5 | 0 |
| images unaccounted | 8 | **1** |
| score | 0.934 | **0.973** |

And an image-only PDF built from a real 1789 scan — five pages with no text layer at
all — reads at **1.000** with every image accounted for, using local OCR at zero
tokens.

**Three of this round's five changes were wrong, and the evaluator said so each time.**
That is worth recording as much as the fixes:

1. Widening the OCR rung's reasons to include `only N characters of content` made it
   fire on thin-but-correct pages. OCR output REPLACES the page's text, so a PDF whose
   whole point is unusual spacing went from 100 % text recall to 0 %.
2. Making the rung decide from signals looked like the fix — but the gate computed its
   character count and never published it. The ladder read the absent key as zero,
   concluded every page was empty, and offered OCR to pages whose problem was two
   columns: 17 deterministic repairs became 17 pages for review, with 14 reporting lost
   text. The gate now publishes `content_chars` and the ladder reads that name.
3. The evaluator's page record was rebuilt without its `provenance` — the field that
   says HOW the text was obtained — so five pages read completely by OCR were reported
   as unaccounted for. Image accounting asks exactly that question.

Each was found by running the corpus and reading the score, not by review, and the
common shape is a contract between two stages that both sides assumed: a key one
writes under one name and the other reads under another, or does not write at all.

### Corpus round 13### Corpus round 13 — documents chosen because they broke something

A new source family: Apache Tika's PDF test-documents, thirty files contributed to a
parser that reads documents for a living, chosen by its maintainers because they
defeated it — annotations with no contents, content streams that space glyphs
unusually, an embedded Flash object, JBIG2-encoded scans, an XFA government form,
four PDF versions of a single blank page.

Offline, with no model calls:

| quantity | value |
| --- | --- |
| documents / pages | 30 / 41 |
| routes | 28 static · **17 repaired deterministically** · 8 review |
| score | **0.962** |
| text recall / tables | 100 % / 100 % |
| residual flags | 8 `image_unaccounted` |

**The first run reported five broken documents and every one was a blank page.** Four
were single-page PDFs whose only content is a blank page; the fifth embeds a file in
an annotation with no contents. Each was recorded as `ocr produced no text on this
page` — a fact about the document reported as a failure of the reader, and tesseract
agrees with the pipeline (zero ink, zero characters).

Two changes, and both are the distinction this project has now had to make four times
— a legitimate outcome recorded as a defect:

- `page_ocr` returns ok=True when it reads a page and finds no text. `ok=False` is
  reserved for "I could not do my job": no engine, no raster, no time.
- the gate does not flag a page with nothing on it. "Nothing drawn" is decidable from
  the page's own objects: no text, no blocks, no paths, no images.

The image count is load-bearing and is taken in the same PDFium pass as the paths,
because a scanned page has no paths and no text — a rule that looked only at paths
would declare every page of a scanned book empty and skip it, turning a content family
into silence. Tests pin both directions: a scan is not empty, a blank leaf is.

| adversarial corpus | before | after |
| --- | --- | --- |
| documents reporting errors | 5 | **0** |
| pages flagged | 13 | **8** |

The eight that remain are `image_unaccounted`: JBIG2 scans, an XFA form and journal
tables whose images are present and linked but not read. Offline that is the honest
result and vision is the remedy, exactly as for Office pictures.

### Corpus round 12 — a page that is an image no longer needs a model

The harvest added a family the corpus never had: govinfo's digitised historical
volumes, starting with the Statutes at Large, volume 1 (1789, 828 pages, 158 MB).
A page that is only an image has no characters for `pdftotext` to return and no fonts
for the trust rule to judge, so the only route the pipeline had for it was the vision
model at ~4 100 tokens a page — a per-page cost that never falls with scale.

tesseract is now in the image and wrapped as `page_ocr`, a local operator with a
wall-clock budget that emits paragraph blocks, reports what it produced, and fails
cleanly so the ladder falls through to the model when OCR cannot manage. The ladder
offers it first whenever the reason is missing or untrustworthy characters.

Verified on an image-only document built from the real scan — five pages rendered to
images and reassembled as a PDF with no text layer at all:

| | value |
| --- | --- |
| pages | 5, all `adapted` |
| gate failures / review | **0 / 0** |
| vision calls / tokens | **0 / 0** |
| text | `Entered according to Act of Congress, in the year 1845, by Cuartes C. Lirrte & James Brown` |

The OCR is not perfect on a 1789 page — that line should read `Charles C. Little` —
but it is readable, it costs nothing, and the model is still there for pages where it
matters.

**And the measurement that corrected the claim.** The operator was added on the
grounds that this volume is 828 pages of pure image with *zero* characters of text
layer. That came from sampling three pages, and all three are blank front matter.
Measured across the volume: **4 300-5 600 characters a page** — govinfo OCR'd the book
when it digitised it. A fresh tesseract pass does not beat that layer:

| page 300 | tokens | word shape | reads |
| --- | --- | --- | --- |
| the volume's own OCR layer | 846 | 0.91 | `Ses s . I. Ch . 24.` |
| fresh tesseract at 300 dpi | 844 | 0.91 | `Sess. I. Cu. 24.` |

Neither is right: the layer inserts spaces, tesseract substitutes characters (`Cu.`
for `Ch.`, `An Met` for `An Act`). Two readers failing differently is not one reader
being better, so the operator is documented as being for documents with NO layer
rather than as a second opinion on a layer that exists.

### Model token usage, measured with the model switched on

The corpus run again with the vision path enabled and a cap of 30 calls, cold cache,
so the numbers are what the pipeline actually spends rather than what it saved:

| quantity | value |
| --- | --- |
| documents / pages | 39 / **1 374** |
| **vision calls** | **19** (1.4 % of pages) |
| prompt tokens | 32 775 |
| completion tokens | 45 220 |
| **total tokens** | **77 995** |
| **per page** | **0.014 calls · 56.8 tokens** |
| per escalated page | ~4 105 tokens |
| pages repaired deterministically instead | **410** (`adapted`) — 22× the number that reached the model |
| pages left for review | 13 |

The counterfactual is what makes the number mean something. Without the deterministic
rungs, the 410 pages they repaired would have joined the 19 that genuinely needed a
model, at ~4 105 tokens each: about **1.76 M tokens**, or 1 281 tokens a page. The
pipeline spends 56.8. That is a **22.5× reduction**, and it is the largest single
effect of the work in this session — larger than the trust-rule correction that
stopped 400-odd GPO pages being sent to vision, because that correction removed a
whole file family while the ladder removes a class of page.

The per-call cost also rose from the 1 806 + 2 096 measured early on to ~2 195 + 3 643,
because attempts after the first now ask for thinking (`reasoning_tokens: 1 836` on a
sampled call). Cheaper per call and rarer, but the honest figure is the one measured
now.

### Corpus round 11 — the corpus evaluates clean

| corpus, 1 374 pages | start of this work | now |
| --- | --- | --- |
| score | 0.968 | **1.000** |
| pages flagged | 218 | **0** |
| text recall | 99.8 % | 100.0 % |
| images accounted | 91.9 % | 100.0 % |
| tables survived | 88.6 % | 100.0 % |

Zero flags means every page passed every check the evaluator makes: text present and
accounted for, images either read or linked, tables extracted where a table exists.
The `garbled` flags that survived the last round (five, all mathematics) are gone
with the shared two-condition rule.

### Corpus round 10 — mathematics is not debris

A fresh 3 207-page corpus reported 166 `garbled` pages. The samples were formula-dense
papers: `σij,ts`, `(eit ejs ) = OP (1)`, `W1 = (H 0⊤ )−1 λi (1)λ⊤ 0 −1 i (1)(H ) = OP (1)`.
Those are not words, and a measure that asks for words calls them debris — but the
document that flag exists to catch scored **0.41**, inside the 0.14–0.56 range those
pages occupy, so no threshold on word shape separates notation from debris.

Font metadata does, which is why the reader has always required both:

| document | unicode ratio | word shape | verdict |
| --- | --- | --- | --- |
| 1950s scan, layer guessed from glyph names | 0.00 | **0.41** | debris |
| 1982 survey | 0.00 | 0.96 | text |
| Congressional Record | 0.00 | 0.95 | text |
| formula-dense arXiv papers | 0.77 | 0.14–0.56 | text |

The evaluator applied word shape on its own. `textstats.layer_looks_misdecoded` is
now the single expression of the decision, called by the reader and the evaluator
alike, and the evaluator takes the font ratio from the session — treating an
unavailable signal as *no evidence* rather than as doubt. Replacement characters
remain a flag on their own: a decoding failure with no ambiguity.

| the three papers carrying 106 of the 166 flags | before | after |
| --- | --- | --- |
| flags | 106 | **0** |
| score (209 pages) | 0.85–0.90 | **1.000** |

Re-evaluated over the whole fresh corpus:

| 100 documents / 3 207 pages | before | after |
| --- | --- | --- |
| `garbled` flags | **166** | **0** |
| pages flagged | 252 | **90** |
| score | 0.987 | **0.992** |

What remains is 69 `table_missing` — artifacts from before the table fix, which the
re-run now in flight should collapse — plus 12 linked-but-unread images, 9
`text_lost` and one empty output.

**The gate was never affected.** No page of the fresh corpus is in review for a
text-shape reason, so this was a measurement defect and not a cost one: the pipeline
had been reading those papers correctly the whole time, and only the number
describing them was wrong. It is the third round in a row where that was true —
tables counted by detection rather than acceptance, text called lost because a
duplicate was removed, and now mathematics called debris.

### Corpus round 9 — the escalated pages were throwing their tables away

Every `table_missing` flag in the corpus turned out to have the same cause, and it
was not the table extractor. The column escalation re-reads a page in reading order
and sets `replace_blocks`, which cleared **every** block on the page — so the
operator that fixed the reading order also discarded the tables, figures and
captions the plan had already extracted. The pages that take that path are exactly
the pages whose plan had found tables.

The flags did not show it; the signals did: on p15 page 32 the run recorded
`tables_found: 1` while the evaluator recorded `output_tables: 0`, and all 41
flagged pages had route `adapted`.

`replace_blocks` now supersedes the prose — paragraph, heading, list — and leaves
structured and visual blocks in place, and the column rung re-runs the plan's
ordering operator afterwards (found by what it produces, `reading_order`, not by
its name).

| on the documents that carried the flags | before | after |
| --- | --- | --- |
| p15.pdf | 80 % | **100 %** |
| 2021.naacl-main.1.pdf | 60 % | **100 %** |
| 2021.naacl-main.3.pdf | 75 % | **100 %** |
| four documents together, score | 0.880 | **0.994** |

Re-run over the whole corpus, 1 374 pages:

| full corpus | before this round | after |
| --- | --- | --- |
| text recall | 100.0 % | 100.0 % |
| images accounted | 100.0 % | 100.0 % |
| tables survived | 97.0 % | **100.0 %** |
| pages flagged | 46 | **5** |
| score | 0.991 | **1.000** |

The five remaining flags are all `garbled`, all on mathematics papers (0.976–0.999).

The corpus's review count moved 10 → 20, and that is the trade working: sparse
tables (50–56 % empty cells) now survive the escalation and the gate flags those
pages for a person, where before the table was deleted and the page passed.

**Evaluation cost.** Table re-detection is 43 s of a 52 s evaluation over 98 pages,
and it ran on every page — including the pages the operator declines by design,
which are the ones with tens of thousands of vector objects. Applying the
operator's own guards took an evaluation of 102 documents and 3 207 pages from
**over 90 minutes (killed, unfinished) to 33 minutes**, and removed the last two
`table_missing` flags, which were pages the operator had declined: they are now
reported as "table expectation not established" rather than as loss.

Threads were tried for it and do not work: 24 document workers gave 95 threads,
**100.7 % of one core** and 6.7 GB resident, because table detection is pure Python
inside pdfplumber and the GIL serialises it. That is reverted, with the measurement
left where the loop lives, and `--fast` (9 s instead of 45 s on the same pages) is
the option that shortens an evaluation.

### Fresh harvest, judged end to end

| quantity | value |
| --- | --- |
| documents / pages | **100 / 3 207** |
| text recall | **100.0 %** |
| images accounted | 99.6 % |
| tables survived | **97.9 %** |
| score | **0.987** |
| flags | 166 `garbled`, 69 `table_missing`, 12 `image_unaccounted`, 9 `text_lost` |

### Corpus round 8, part two — a fresh harvest, judged by the corrected evaluator

103 documents / 238.8 MiB harvested in one pass across 16 archetypes (24 two-column
papers, 16 table-heavy, 8 formula-heavy, 8 figure-led, 6 ASCII-diagram RFCs, 8 real
decks, 8 real Word files, forms, statutes, a 300-page register). The largest 37 of
them — every document over ~1 MiB, none of them previously seen — were run and
evaluated end to end:

| quantity | value |
| --- | --- |
| documents / pages | 37 / **1 107** |
| text recall | **100.0 %** |
| images accounted | **100.0 %** |
| tables survived | **96.8 %** |
| score | **0.990** |
| flags | 36 — every one `table_missing`, no `garbled`, no `text_lost` |

The whole harvest then ran in one pass, 103 files in 35.4 minutes, clean exit:

| quantity | value |
| --- | --- |
| documents / pages | **102 / 3 207** |
| routes | 2 114 static · **837 adapted** · 208 dynamic · 48 needs review |
| model tokens | **0** |
| rejected, correctly | 2 (a `.docm` and a fuzzer artifact) |

3 207 pages of documents the pipeline had never seen, read without a single model
call, with 837 pages repaired by a deterministic operator after their first plan
failed their gate, and 48 left for a person. The evaluator is running over the
whole set.

Two of those zeros are this round's work: the garbling measure and the text-loss
measure now report what they claim to. The remaining gap is tables, and it is
consistent across corpora — ACL and EMNLP two-column papers sit at 60–88 % table
survival because their tables are set with rules the line strategy finds and the
extraction still misses.

### Corpus round 8 — the "lost text" was repetition, and probing ran ahead of reading

**The 39 pages flagged `text_lost` were not losing text.** Measuring what was
missing from them, token by token:

| quantity | value |
| --- | --- |
| tokens missing from the 39 flagged pages | 571 |
| present elsewhere in the same document | **560 (98.1 %)** |
| absent from the document entirely | **11** — numeric literals |

The dominant pattern was an RFC's page header (`rfc 8446 tls august 2018`), removed
because it repeats on every page while the first occurrence is kept. A deliberate
transformation was sharing a column with a real defect, and the 11 tokens that were
genuinely gone were invisible inside the count. `recall_split` now separates them,
recall credits a token the document keeps elsewhere, and document-level recall is
reported beside the per-page averages.

| on the full corpus | before this round | after |
| --- | --- | --- |
| text recall | 99.8 % | **100.0 %** |
| pages flagged | 85 | **46** |
| `text_lost` | 39 | **0** |
| score | 0.987 | **0.991** |

**Probing ran ahead of reading.** Preparing a document means probing it, and that
ran for all 103 documents of the new harvest before any page was extracted: about
eight minutes of silence, with every probed document held in memory. Measured on a
163 MiB, 12-document corpus, time to first output:

| | in the first 100 s |
| --- | --- |
| prepare-all-then-extract | **nothing started** |
| interleaved | **first document finished** |

Sharding keeps the up-front pass, because it partitions (document, window) pairs
across processes and needs the whole list.

### Corpus round 7 — the full corpus completes, and two measures were wrong

| quantity | value |
| --- | --- |
| run | **39 documents / 1 374 pages**, offline, clean finish (first complete full-corpus run) |
| routes | 889 static · **413 adapted** · 62 dynamic · 10 needs review |
| model tokens | **0** (917 pages resolved deterministically without a model) |
| evaluator, before this round's evaluator fixes | score 0.968 · text 99.8 % · images 100 % · tables 92.5 % |
| evaluator, after | **score 0.987 · text 99.8 % · images 100 % · tables 97.0 %** |

The two evaluator corrections moved the measurement, not the extraction: flags went
110 → **5** for `garbled` and 104 → **41** for `table_missing`, and the score
0.968 → 0.987. `text_lost` (39 pages) is unchanged and is the next real
content-quality item.
| wall clock | 39 documents in ~23 min, ~1 s/page (a 27-file Office sample: 78 s) |

**The segfault is found and fixed.** A corpus run had died three times with exit
139 (SIGSEGV, `OOMKilled=false`) while extracting the 357-page Federal Register as
the 16th document, taking the run's artifacts with it. PDFium is not thread-safe
and the runner pools threads over page windows; every PDFium call in the session
held `_PDFIUM_LOCK` except one — `page_lines`, the path the column escalation
takes, loaded its page one line above the lock:

    page = self._pdf[page_no - 1]      # FPDF_LoadPage, UNLOCKED
    with _PDFIUM_LOCK:
        textpage = page.get_textpage()

It went unnoticed because `self._pdf[n]` is a call through `__getitem__` and an
audit of parenthesised call sites never sees it. `tools/check_pdfium_lock.py` now
walks the AST for call nodes and subscripts alike and runs as a test over every
module that mentions PDFium. With it fixed, the Federal Register completes at the
position that killed three runs, and the corpus finishes end to end.

**Two measures were measuring the wrong thing**, both in the evaluator:

- `table_missing` counted every rule-bounded region pdfplumber returns, while the
  operator rejects single-row boxes, one-column lists and empty frames on purpose.
  68 pages were flagged, five of which had emitted a table block at all.
  `table_rows_accepted` is now the single definition, imported by the evaluator.
- `garbled` counted alphanumeric characters over ALL characters, which counts
  newlines and indentation: a chart page scored 0.27 ("garbled") because its text
  is one number per line, and it flagged 110 pages whose samples were charts,
  result tables, a roll-call list and a letterspaced masthead. Meanwhile the one
  genuinely mis-decoded document to hand scored 0.90 on it. Word shape — the
  measure the reader already uses for the same question — separates them cleanly:

  | document | word shape | verdict |
  | --- | --- | --- |
  | topology paper, mis-decoded | **0.41** | flagged |
  | 1982 survey | 0.92 | not flagged |
  | 2021 ACL paper | 0.92 | not flagged |
  | Congressional Record | 0.95 | not flagged |
  | IRS form 1040 | 0.86 | not flagged |

  Both callers now import `docpipe.textstats.text_word_shape`, because two
  definitions of one idea drifted apart and neither was aware of the other.

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
| One PDFium call outside the lock (`page_lines` page load) | a 357-page document segfaulted the process (exit 139) as the 16th document of a run, three times; the same document alone succeeded. Missed by an audit of parenthesised call sites, because `self._pdf[n]` is `__getitem__` |
| The evaluator counted pdfplumber detections as expected tables | 68 pages flagged `table_missing` for single-row boxes, one-column lists and empty frames the operator rejects by design |
| The evaluator's garbling check divided by all characters | flagged 110 chart, table and roll-call pages as debris while the one mis-decoded document scored 0.90 on the same measure |
| A `notes` field was appended to without being initialised | `NameError` inside the document report; caught by the suite before it reached a run |
| The probe harness tested a remote path with a local `[ -d ]` | always false, so probes silently ran the copy baked into the image instead of the working tree |
| The sequential window path had no failure isolation | the pooled path caught a failing window and the single-window path did not, so a file that vanished mid-document exited 1 with 40 of 80 pages unread |

### A crash no longer takes the finished work with it

The corpus run died twice at the same place with **exit 139 — SIGSEGV**, not OOM
(`OOMKilled=false`, 45-66 MiB peak against a 24 GiB limit), while extracting the
357-page Federal Register as the 16th document. The same document completes alone
in 278 s, so the trigger is the document in sequence rather than the document. It
is reproducible and is the next thing to fix.

What it exposed is arguably worse than the crash: **15 completed documents became
unreadable**, because `pages.parquet` is written as a stream and parquet cannot be
read without its footer. Two changes now make a fatal crash survivable:

- every document also writes a small, complete shard (`shards/<doc_id>.pages.parquet`),
  and each finished document is appended to `documents.jsonl` as it completes;
- the run report is rewritten after every document, marked `IN PROGRESS` until the
  run finishes cleanly — so a stale report says it is stale instead of looking
  complete.

`docpipe recover --out <dir>` merges the shards back into `pages.parquet` and
`blocks.parquet`, and the evaluator then judges the documents that did finish.

### Extraction quality did not move backwards

Comparing the same 13 documents and 137 pages, evaluated by the same evaluator
against the older and the current code:

| on 137 shared pages | earlier | current |
| --- | --- | --- |
| pages flagged | 32 | **31** |
| text recall | 0.9987 | **0.9991** |
| `text_lost` | 1 | **0** |
| `garbled` | 3 | 3 |

The wider comparison is not valid and was worth checking for exactly that reason:
the recovered subset is 268 pages and the earlier full run 571, so per-page flag
counts differ because the page sets differ. Flag rates must be compared on the
same pages.

## Open findings

- **A defective OCR layer is trusted, and no local measure detects it — measured,
  not assumed.** The 1789 volume's layer carries a Unicode map, so the trust rule has
  no doubt to report; recall against itself is 100 %; word shape is 0.91-0.96. Three
  candidate detectors were tried and all three failed:

  | page | image area | single-letter rate | agreement with a fresh OCR |
  | --- | --- | --- | --- |
  | STATUTE-1 p80, layer reads `Ses s . I. Ch . 5.` | 1.00 | 0.033 | **0.978** |
  | STATUTE-1 p300, layer reads `Ses s . I. Ch . 24.` | 1.00 | 0.046 | **0.973** |
  | Congressional Record p3, clean, born-digital | 0.00 | 0.042 | 0.973 |
  | 2021 ACL p3, clean, born-digital | 0.00 | 0.111 | 0.309 |
  | Federal Register p3, clean, born-digital | 0.00 | 0.009 | 0.210 |

  A second reader agrees with the DEFECTIVE layer at 0.97 and disagrees with two
  clean documents at 0.21-0.31, because tesseract makes the same substitutions on
  18th-century type (`Cu.` for `Ch.`) that the volume's own OCR made. Two readers
  failing alike is not corroboration, and no reference-free, dictionary-free statistic
  separates them. Detecting this needs a dictionary, a language model, or a person.

  What the numbers do say is how big the defect is: agreement 0.97 and a token count
  within 0.5 % of a fresh pass mean the layer is ~97 % right with local damage, which
  is a proofreading problem rather than an extraction failure. The pipeline reports
  what it read; it does not claim to have proofread it.
- **166 pages of the fresh harvest report `garbled`** (5 %), concentrated in the
  formula-heavy arXiv papers. The pipeline's trust rule uses the same word-shape
  measure, so this may also be driving the 48 `needs_review` pages — it is the next
  thing to measure, and the question is the same one that has been asked twice
  already: is the text debris, or is the measure reading mathematics as debris?
- **Sixty-nine pages report `table_missing`** — a table the evaluator can see in
  the source that produced no table block — down from 104 before the evaluator
  applied the operator's own acceptance rule, and the largest remaining
  content-quality gap. The weakest documents are ACL papers at 58-75 % table
  survival: their tables are set with rules the line strategy finds, and the
  extraction still misses them.
- **Office pictures are linked, not read.** The evaluator's `image_unaccounted`
  flags on decks are correct by design: a figure block that points at a file has
  not been read. The vision path is the remedy, and offline runs leave them for
  review. What this round fixed is that the bytes were missing entirely for slides
  holding more than one picture.
- **Forms report partial table survival.** `f1040` and `f1120` are grids of boxes and the
  evaluator expects the grid, but most of a form's cells are empty by design and
  the acceptance rule rejects tables that are more than 60 % empty. Whether a form
  should produce a table block, a set of field blocks, or both is an open design
  question, not a bug to patch. `f1040` moved from 0 % to 50 % table survival
  once the evaluator applied the operator's own acceptance rule.
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
