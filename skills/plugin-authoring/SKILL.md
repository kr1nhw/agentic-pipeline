---
name: plugin-authoring
description: Write, install, lint or debug a docpipe extension — a Python format/op/pipeline plugin, or a DSH plugin that adds agent tools. Load before creating or installing any extension.
---

# Authoring plugins

Two extension layers, one rule: **a plugin registers through the same API the builtins
use**. If you find yourself special-casing a builtin, the API is wrong — fix the API.

| Layer | What it extends | How it installs |
| --- | --- | --- |
| **docpipe plugin (Python)** | formats, operators, pipelines, exporters, hooks | entry point `docpipe.plugins`, `$DOCPIPE_PLUGIN_PATH`, `/plugins`, or `--plugin <path>` |
| **DSH plugin (Node/TS)** | the agent: new tools, services, prompt sections, UI | `dsh plugin --profile docpipe add <package>` |

The bridge between them is deliberate and one-directional: a DSH plugin adds an agent tool,
and that tool drives the Python operator registry through the CLI
(`docpipe ops call <op> --json`) or the local service (`docpipe serve`). Never reimplement
extraction in JavaScript.

## Python plugin skeleton

```
acme-csv/
  docpipe-plugin.toml
  acme_csv/__init__.py
```

```toml
id = "acme-csv"
version = "1.2.0"
api_version = "1"
provides = ["format", "op"]
capabilities = ["filesystem_read"]
requires_ops = []
```

```python
from docpipe.api import registry, OpResult

def match(head: bytes, path: str) -> float:
    return 0.9 if path.lower().endswith((".csv", ".tsv")) else 0.0

def enumerate_units(path, ctx):
    # yield one unit dict per page/slide/section; stream, never build a list
    ...

def op_rows(page, params, ctx):
    return OpResult(blocks=[...], signals={...})

def register(api: registry.PluginAPI) -> None:
    api.register_format("csv", match=match, enumerate_units=enumerate_units, kind="text_chunk")
    api.register_op("csv_rows", fn=op_rows, cost_class="local",
                    params_schema={"delimiter": {"type": "string", "default": ","}})
```

```toml
# pyproject.toml
[project.entry-points."docpipe.plugins"]
acme_csv = "acme_csv:register"
```

## Capabilities are enforced, not declared

The context a plugin receives is a capability-checked proxy. A plugin that declares no
`model_calls` cannot spend tokens; no `subprocess` cannot shell out; no `filesystem_write`
cannot write outside its scratch directory. Attempting it raises `CapabilityError` and the
violation lands in `report.json`. Declare the narrowest set that works — a plugin asking for
`model_calls` is asking to spend the operator's money, and that should be visible in review.

## Non-negotiables

- **Stream.** A format plugin yields units and must never materialise a whole file. Assume a
  4 GB input and a 4 GB memory cap (`--max-rss-mb`). Declare `seekable` and
  `max_unit_bytes` so the runtime can shard safely.
- **Cost class honestly.** `free` (pure python), `local` (CPU, may be slow), `vl` (calls a
  model). Mislabeling a `vl` op as `local` breaks budget enforcement.
- **Determinism.** Same input + same params ⇒ same output. Anything sampling or
  time-dependent must be seeded from the content hash.
- **Fail loudly, return validated results.** An `OpResult` that fails schema validation
  twice disables your plugin; that is preferable to corrupting a corpus.
- **No network in `free`/`local` ops.** Network belongs to declared `vl` ops so the rate
  limiter and budget can see it.

## Workflow

```bash
docpipe plugin lint ./acme-csv      # manifest, api_version, imports, capability honesty
docpipe plugin install ./acme-csv
docpipe plugin list                 # loaded plugins, versions, capabilities, violations
docpipe ops --markdown | grep csv   # confirm your ops entered the registry
```

Lint before install, run `docpipe selftest` after, and re-run `docpipe ops --markdown` so
the generated registry the planner reads stays in sync with reality.
