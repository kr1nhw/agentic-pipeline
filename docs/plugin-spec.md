# Plugin API v1

docpipe extends in two layers. They exist for different reasons, and keeping them
separate is what stops the agent layer and the extraction layer from growing into
each other.

| Layer | Extends | Installed with | Runs in |
| --- | --- | --- | --- |
| **docpipe plugin** | formats, operators, pipelines, exporters, hooks | entry point `docpipe.plugins`, `$DOCPIPE_PLUGIN_PATH`, `/plugins`, `--plugin <dir>` | the Python runtime, in-process |
| **DSH plugin** | the agent: new tools, services, prompt sections | `dsh plugin --profile docpipe add <npm-pkg>` | the harness, alongside the agent |

A DSH plugin does not reimplement extraction. It adds an agent tool that drives
the Python operator registry through `docpipe ops call <op>` or the local
`docpipe serve` endpoint. The Python layer stays the single source of truth for
what an operator *is*.

## The contract

**A plugin is a directory with a manifest and a `register(api)` function.**

```
acme-csv/
├── docpipe-plugin.toml
└── acme_csv/
    └── __init__.py        # def register(api): ...
```

```toml
id = "acme-csv"                # unique; becomes the plugin id on every registration
version = "1.2.0"              # semver of the plugin itself
api_version = "1"              # major of THIS spec; a mismatch refuses to load
module = "acme_csv"            # optional; defaults to the id with dashes as underscores
provides = ["format", "op"]    # documentation: what the plugin intends to add
capabilities = ["filesystem_read"]

# Dependencies are declarative and checked at load: an operator that needs
# `pdf_tables` and cannot find it fails loudly instead of quietly doing less.
requires_ops = []
```

Or, when distributing through PyPI, drop the manifest and use an entry point:

```toml
[project.entry-points."docpipe.plugins"]
acme_csv = "acme_csv:register"
```

The loader accepts both. Manifest fields are the same either way; an entry-point
plugin that needs capabilities declares them in the manifest beside it, because a
capability is a promise to the operator, not a packaging detail.

## `register(api)`

```python
from docpipe.api import Capability, OpResult

def register(api) -> None:
    api.register_format(
        "csv",
        match=match,                      # (head: bytes, path: str) -> 0.0..1.0
        enumerate_units=enumerate_units,  # (FormatContext) -> Iterator[UnitSeed]
        kind="text_chunk",
        extensions=(".csv", ".tsv"),
        default_ops=("text_passthrough",),
        seekable=True,                    # unit N needs no unit < N
        streaming=True,
    )
    api.register_op(
        "csv_rows",
        csv_rows,
        summary="Emit CSV rows as table blocks. …",
        params_schema={"delimiter": {"type": "string", "default": ","}},
        cost_class="local",
    )
    api.register_pipeline(spec)           # a PipelineSpec, usually loaded from YAML
    api.register_exporter("acme-json", export_rows)
    api.register_hook("post_verify", on_verified, priority=50)
```

### `register_format`

| Field | Meaning |
| --- | --- |
| `match(head, path)` | confidence 0..1. Return 0 to decline. Match on bytes; extensions lie. |
| `enumerate_units(ctx)` | yield `UnitSeed`s, streaming. Fill tier-1 signals as you go — they are free and routing depends on them. |
| `kind` | `pdf_page` \| `slide` \| `docx_flow` \| `text_chunk` \| `image` \| `code_unit` |
| `seekable` | **A promise.** True means unit N can be produced without reading units `< N`. The runtime shards on it. |
| `streaming` | True means the source can be consumed once; the runtime will not re-enumerate for a second pass. |
| `session_factory` | Optional. `(path, doc_id, config) -> session`. Without it the format cannot rasterise, so no `vl_*` operator can be used on it. |

### `register_op`

```python
def my_op(ctx: OpContext, params: dict) -> OpResult:
    ...
```

| Field | Meaning |
| --- | --- |
| `summary` | **Read verbatim by the planner.** Say what it does and when it is the wrong tool. |
| `params_schema` | Per-property dict: `type`, `default`, `enum`, `min`, `max`, `required`. Validated before every call; unknown parameters are rejected. |
| `cost_class` | `free` (pure Python) · `local` (CPU, filesystem, subprocess) · `vl` (calls a model). Mislabeling a `vl` op breaks budget enforcement. |
| `requires` | Artifacts that must already exist, e.g. `("raster",)`. The planner cannot order a `vl_extract` before a `page_raster`. |
| `produces` | What it adds: `blocks`, `table`, `figure`, `raster`. |
| `unit_kinds` | Restrict to `pdf_page`, `slide`, … |

An operator returns `OpResult`:

```python
OpResult(
    blocks=[...],            # appended in order; `order` is reassigned by the executor
    replace_blocks=False,    # True: this supersedes what earlier operators produced
    signals={...},           # merged onto the page's signals
    artifacts={"raster": "/work/.../p1@150.png"},
    cost=Cost(vl_calls=1, prompt_tokens=..., completion_tokens=...),
    ok=True,
    diagnostics=["why, if not ok"],
    metadata={...},          # not written to parquet; `quality` and `chunks` are honoured
)
```

Two `metadata` keys are load-bearing: `metadata["quality"]` sets the gate verdict,
and `metadata["chunks"]` carries chunk output.

## Capabilities

`capabilities = ["filesystem_read", "subprocess", "model_calls", ...]` from
`filesystem_read`, `filesystem_write`, `subprocess`, `network`, `model_calls`.

**Enforced:**

- `cost_class="vl"` cannot be registered by a plugin without `model_calls` —
  registration raises `CapabilityError`.
- The executor passes `ctx.vl` only to operators whose plugin declared
  `model_calls`. Without it there is no client, so there is no way to spend money,
  by construction rather than by trust.
- `ctx.run(...)` raises unless the plugin declared `subprocess`.

**Not enforced:**

- Filesystem writes. A plugin runs in-process with the runtime's privileges.
  `filesystem_write` is a review signal that makes the demand visible in
  `docpipe plugin list` and `report.json`, not a jail.

Stating that plainly is more useful than implying a sandbox that does not exist.

## Guarantees the runtime makes to a plugin

- **Your operators are called with validated parameters.** Defaults are applied
  and unknown keys rejected before you are invoked.
- **Your operator is not called for a unit kind it did not declare.**
- **A raised exception fails one unit, not the run.** The page is marked failed
  with your traceback in `errors`.
- **Your registration failure disables only your plugin.** It is reported in
  `report.json` and `docpipe plugin list`.
- **A `vl` result is cached** against `(page content, op, params, model)`, so a
  re-run does not re-buy it.

## Guarantees a plugin must make

- **Stream.** `enumerate_units` yields; a plugin must never materialise a whole
  file. Assume a 4 GB input and a 4 GB memory cap.
- **Be deterministic.** Same input and parameters ⇒ same output. Seed anything
  random from the content hash.
- **Report honestly.** An `ok=False` with a diagnostic is worth more than a
  plausible wrong answer: the gate escalates on failure and would not on a lie.
- **Respect `requires`.** Declare the artifacts you need; do not open the document
  yourself to work around a missing raster.

## Validation

```bash
docpipe plugin lint ./acme-csv     # manifest, api version, schema, capability honesty
docpipe plugin list                # loaded plugins, versions, capabilities, violations
docpipe ops --with-plugins --markdown | grep csv
```

`lint` loads the plugin into an empty registry and validates it plus any pipeline
it registers. Run `docpipe selftest` afterwards: it builds the full registry and
validates the catalog, so a plugin that breaks a shipped pipeline is caught
before a run rather than during one.
