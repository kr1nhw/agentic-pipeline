# Operations

Everything here assumes the image is already built. For build details, including
the base-image and apt findings that make this host unusual, see the comments in
`Dockerfile` and `tools/build_image.sh`.

## Build

```bash
tools/build_image.sh                # rsync context to the remote host and docker build
tools/build_image.sh --no-cache     # full rebuild
tools/sync_data.sh                  # copy ./dataset to the run host
```

The build host facts that shape the build:

| Fact | Consequence |
| --- | --- |
| Docker Hub is unreachable | Bases come from `public.ecr.aws`, which serves Docker manifest lists that docker 18.09 can consume |
| No buildx | The Dockerfile is classic-builder compatible: no `# syntax=`, no `RUN --mount`, no heredocs |
| `python:3.12-slim-bookworm` ships `gpgv` but not `gpg`, and apt delegates verification to the deprecated `apt-key`, which needs `gpg` | Bootstrap install uses `docker/apt-key-gpgv-shim.sh`; `gnupg` is installed in that same transaction and the shim is removed afterwards |
| Sources must be deb822 | Only deb822 carries `Signed-By`; a classic `sources.list` makes apt verify with no keyring at all |
| Containers inherit umask 0027 | Every RUN that creates files sets `umask 0022`, and `/opt/venv` + `node_modules` get `chmod -R a+rX`, or the unprivileged runtime user cannot even traverse them |
| ghostscript's `libpaper1` fails to configure here | ghostscript is off by default (`--build-arg INSTALL_GHOSTSCRIPT=1` to enable); PDFium is the primary rasteriser |

## Run

```bash
tools/run_remote.sh run --in /in --out /out --offline
tools/run_remote.sh run --in /in --out /out --max-vl-calls-per-run 120
tools/run_remote.sh --it dsh                     # the agent, interactively
```

Or directly:

```bash
docker run --rm \
  --memory 8g --cpus 12 --user "$(id -u):$(id -g)" \
  -v /data/mm-lbfs/dataset:/in:ro \
  -v /data/mm-lbfs/out/run1:/out \
  -v /data/mm-lbfs/work:/work \
  -e DEEPSEEK_API_KEY \
  docpipe-agent:0.1.0 docpipe run --in /in --out /out
```

`--user` matters: bind mounts are owned by the calling user, and the image runs as
uid 1000 by default. The entrypoint warns rather than failing obscurely when a
mount is unwritable.

## Running the agent

```bash
docker run --rm -it --user 1000:1000 \
  --security-opt seccomp=unconfined \
  -v /data/mm-lbfs/dataset:/in:ro -v /data/mm-lbfs/out:/out \
  -e DEEPSEEK_API_KEY \
  docpipe-agent:0.1.0 dsh --profile headless "extract the tables from /in/x.pdf"
```

`--security-opt seccomp=unconfined` is required for the agent and only for the
agent. Docker 18.09's default seccomp profile blocks `statx`, glibc's async
`realpath` uses it, and Cordis HMR calls `realpath("<profile>/")` at boot — so
without the flag every profile aborts with `EPERM: operation not permitted,
realpath '/opt/dsh-home/profiles/<name>/'`. The extraction runtime stays inside
the default profile.

## What a run costs

Start with `probe`, which extracts nothing:

```bash
docpipe probe --in /in --summary
```

It prints each document's format, unit count, routed pipeline, confidence and the
near-miss candidates. Documents with a pipeline are free to extract; the rest will
consult the planner.

Then measure before spending:

```bash
docpipe run --in /in --out /out --offline       # the free baseline
```

Read `out/report.json`. `totals.route_counts` shows how many units went down each
path, and `gate_failures` is the only number that justifies vision spend. The
budget flags are hard stops, and exhaustion is a clean, resumable stop:

```bash
docpipe run --in /in --out /out \
  --max-vl-calls-per-run 200 --max-tokens-per-run 3000000 --max-usd 5
```

`DOCPIPE_PRICE_PROMPT_PER_MTOK` / `DOCPIPE_PRICE_COMPLETION_PER_MTOK` set the
price table used for `usd_est`. They default to zero, so the run reports tokens and
leaves the costing to you: an invented number is worse than no number.

## Scale

| Situation | Setting |
| --- | --- |
| One big document, one big machine | `--workers $(nproc)` — windows parallelise within the document |
| Many containers, no coordinator | `--shard i/N` on each, same `--out`; partitions are hash-based, so membership does not depend on which other files are present |
| Very uneven unit costs | Run fewer, larger shards; the op cache makes re-work cheap |
| Memory-limited container | `--max-rss-mb` plus `docker run --memory`; the two must agree |
| Raster growth | `DOCPIPE_RASTER_CACHE_MB` (default 2048) evicts oldest-first, per document |

Determinism check before trusting a large run: extract a slice single-container,
then again with `--shard 0/3`, and diff `blocks.parquet`. Identical output means no
operator is leaking state between units.

## Resume

Unit-level progress lives in the artifacts, and every operator result is cached
against `(page content, op, params, model)`. Re-running a completed corpus makes
zero model calls and rewrites byte-identical artifacts:

```bash
docpipe run --in /in --out /out --resume
```

A killed worker loses at most one window.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `docpipe: not found` in the container | The venv is not traversable — rebuild; the Dockerfile chmods `a+rX` |
| `WARNING: /out is not writable` | Add `--user "$(id -u):$(id -g)"` or chown the mount |
| Every page `needs_review`, zero vision calls | No `DEEPSEEK_API_KEY`, or `--offline`, or a budget stop; `report.json.totals.stopped_reason` says which |
| `model returned only reasoning content` | The answer was crowded out by reasoning; raise `max_tokens` or disable thinking |
| Vision disagrees with the text layer | Expected and reported: `vl_cross_check` records the overlap, and the text layer is authoritative |
| A pipeline silently does nothing | It cannot: the catalog is validated at startup, and an unknown operator or bad parameter fails the run before it starts |
| A plugin breaks a run | It cannot: registration failures disable that plugin only and appear in `report.json` and `docpipe plugin list` |
| `dsh` aborts with `user patch-layer watching requires the Cordis HMR service` | The harness dependencies floated. `dsh` caret-ranges its Cordis deps, so a fresh install picks newer ones than the release was built against and HMR never registers its service. The image pins the set (`ARG DSH_PINS`); re-derive it whenever `DSH_VERSION` changes |
| `dsh` aborts with `EPERM … realpath '/opt/dsh-home/profiles/…/'` | Docker 18.09's seccomp profile blocks `statx`. Add `--security-opt seccomp=unconfined` |

## Self-check

```bash
docker run --rm docpipe-agent:0.1.0 selftest        # modules, PDF backend, registry, catalog
docker run --rm docpipe-agent:0.1.0 pipelines validate
docker run --rm docpipe-agent:0.1.0 ops --markdown | head -40
```
