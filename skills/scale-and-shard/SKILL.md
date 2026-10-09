---
name: scale-and-shard
description: Run docpipe over large files, large corpora or many containers — memory ceilings, windows, sharding modes, the work queue, resume semantics and API backpressure. Load before any run bigger than a handful of files.
---

# Running at scale

The failure mode at scale is not slowness, it is a container that eats memory and dies
mid-corpus, leaving output nobody can trust. Everything here exists to make a run
*bounded*, *resumable* and *repeatable*.

## Bounded by construction

- Ingestion is streaming: PDFium random access means page 900 needs no page 899, one
  document handle per worker, closed promptly. Units flow through a bounded queue
  (`--pages-in-flight`, default 64) — never a whole-document object graph.
- Rasters go to disk under `$DOCPIPE_WORK/rasters` with an LRU budget
  (`--raster-cache-mb`, default 2048) and are deleted at unit completion unless
  `--keep-rasters`. Rasters are the usual cause of a runaway container.
- Writers stream: parquet row groups flush every 256 pages, JSONL appends. Nothing is
  collected to a DataFrame first.
- Each unit has its own limits — `--unit-timeout-s` (default 300), `--file-timeout-s` —
  and a poisoned unit is marked `failed` with its traceback instead of killing the run.
- `--max-rss-mb` sets `RLIMIT_AS` in every child. Pair it with a container limit:
  `docker run --memory 4g … --max-rss-mb 3072`.

## Three ways to use 48 cores

1. **In one container** (default): a process pool over work units. `--workers N` for
   CPU-bound local operators, `--vl-workers M` for network-bound model calls — they are
   separate pools because they saturate different resources. Defaults: workers = cores/2,
   vl-workers = 8.
2. **Many containers, no coordinator** (`--shard i/N`): work units are ordered
   deterministically and partitioned by hash, each shard writes idempotent per-unit outputs
   and merges locally. Same input ⇒ same partition ⇒ identical results to a single run.
3. **Many containers, one queue** (`--queue`): a SQLite WAL lease table at
   `$DOCPIPE_OUT/state/queue.db`. Best when unit cost varies a lot (a 1400-page scan next to
   a two-page note), because workers pull instead of being assigned. Leases expire, so a
   killed container's units are re-claimed; the op cache makes re-work cheap.

```bash
# 3 containers, no coordinator
for i in 0 1 2; do
  docker run -d --name docpipe-$i --memory 4g \
    -v /data/in:/in:ro -v /data/out:/out -e DEEPSEEK_API_KEY \
    docpipe-agent:0.1.0 docpipe run --in /in --out /out --shard $i/3 --resume
done
docker wait docpipe-0 docpipe-1 docpipe-2
docpipe merge --out /data/out          # deterministic reduce, safe to re-run
```

## Giant documents

A 1428-page PDF is planned in 200-page windows: a document-level plan seeds each window,
and a window may request one refinement call if its gate-failure rate exceeds 25 %.
Windows are independent work units, so they parallelise across processes and containers,
and the merge is a deterministic reduce ordered by `page_id`.

Before a long run, measure: extract the first 20 pages offline, read `report.json`, and
extrapolate cost. Do not discover the shape of a corpus by paying for all of it.

## Spending model calls responsibly

- A global token-bucket rate limiter, a retry budget with jitter, and a circuit breaker
  guard the provider. A 429 storm degrades to deterministic-only rather than hammering.
- Budgets are hard stops, checked per page and per document: `max_vl_calls_per_run`,
  `max_tokens_per_run`, `max_usd`. Exhaustion is a clean, resumable stop — remaining units
  stay queued.
- `--offline` disables every `vl` op. A run that cannot succeed offline is a run whose plan
  is wrong; fix the plan, do not buy tokens.

## Resume and repeatability

- Unit status is persisted, so `--resume` skips completed units. The op cache is keyed by
  `(content hash, op, params, backend, model)`, so re-running a completed corpus makes zero
  API calls and rewrites byte-identical artifacts.
- Determinism check before trusting a large run: extract a 20-page slice single-container,
  then as `--shard 0/3`, and diff `blocks.parquet`. They must be identical. If they are not,
  an operator is leaking state between units — fix that before scaling further.

## Oversized inputs

`--max-file-size-gb` (default 4) refuses anything larger unless a plugin provides byte-range
units for that format (`seekable = true`). Audio and video arrive as an extension-point
plugin in this version, not as a promise.
