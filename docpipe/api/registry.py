"""The operator/format/pipeline registry and the plugin API (v1).

This module is the extension standard. Builtins register through the same
:class:`PluginAPI` a third-party plugin receives, so there is no privileged path
that a plugin cannot take and no capability a plugin cannot reach — the only
differences are the declared capabilities and the plugin id recorded on every
registered object.

What is *enforced*, rather than merely declared:

* ``cost_class="vl"`` cannot be registered without ``Capability.MODEL_CALLS``.
* The executor hands ``ctx.vl`` only to an operator whose plugin declared
  ``MODEL_CALLS``, so an undeclared plugin has no way to spend money.
* ``ctx.run`` (subprocess) raises unless the plugin declared ``SUBPROCESS``.

What is declared but not sandboxed: filesystem writes. A plugin runs in-process
with the runtime's privileges, so ``FILESYSTEM_WRITE`` is a review signal, not a
jail. :mod:`docpipe.api.types` says so too; the honest statement is more useful
than the pretence of a sandbox.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import importlib.util
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from docpipe import API_VERSION
from docpipe.api.params import describe_schema, validate_params
from docpipe.api.types import (
    Capability,
    CapabilityError,
    FormatSpec,
    HookFn,
    OpSpec,
    PluginError,
)
from docpipe.schema import PipelineSpec

#: Hook points a plugin may attach to, in execution order.
HOOK_POINTS = ("post_ingest", "pre_plan", "post_verify", "on_error")

ENTRY_POINT_GROUP = "docpipe.plugins"

PLUGIN_MANIFEST = "docpipe-plugin.toml"


@dataclass
class PluginInfo:
    """What was loaded, from where, and what it was allowed to do."""

    id: str
    version: str = "0"
    api_version: str = API_VERSION
    source: str = "builtin"
    capabilities: frozenset[Capability] = frozenset()
    provides: tuple[str, ...] = ()
    ops: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    pipelines: list[str] = field(default_factory=list)
    exporters: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    disabled: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "api_version": self.api_version,
            "source": self.source,
            "capabilities": sorted(c.value for c in self.capabilities),
            "ops": len(self.ops),
            "formats": self.formats,
            "pipelines": self.pipelines,
            "exporters": self.exporters,
            "hooks": self.hooks,
            "violations": self.violations,
            "disabled": self.disabled,
            "error": self.error,
        }


class Registry:
    """The one place every operator, format, pipeline, exporter and hook lives."""

    def __init__(self) -> None:
        self.ops: dict[str, OpSpec] = {}
        self.formats: dict[str, FormatSpec] = {}
        self.pipelines: dict[str, PipelineSpec] = {}
        self.exporters: dict[str, Callable[..., Any]] = {}
        self.hooks: dict[str, list[HookFn]] = defaultdict(list)
        self.plugins: dict[str, PluginInfo] = {}
        self._plugin_caps: dict[str, frozenset[Capability]] = {}

    # ── registration ────────────────────────────────────────────────────────

    def register_op(self, spec: OpSpec) -> None:
        if spec.name in self.ops:
            raise PluginError(f"operator {spec.name!r} is already registered")
        if spec.cost_class == "vl" and Capability.MODEL_CALLS not in self.capabilities_for(spec.plugin):
            raise CapabilityError(
                f"operator {spec.name!r} declares cost_class='vl' but plugin {spec.plugin!r} "
                f"does not declare the 'model_calls' capability"
            )
        self.ops[spec.name] = spec
        self._plugin_info(spec.plugin).ops.append(spec.name)

    def register_format(self, spec: FormatSpec) -> None:
        if spec.id in self.formats:
            raise PluginError(f"format {spec.id!r} is already registered")
        self.formats[spec.id] = spec
        self._plugin_info(spec.plugin).formats.append(spec.id)

    def register_pipeline(self, spec: PipelineSpec) -> None:
        if spec.id in self.pipelines:
            raise PluginError(f"pipeline {spec.id!r} is already registered")
        self.pipelines[spec.id] = spec

    def register_exporter(self, name: str, fn: Callable[..., Any], plugin: str = "builtin") -> None:
        if name in self.exporters:
            raise PluginError(f"exporter {name!r} is already registered")
        self.exporters[name] = fn
        self._plugin_info(plugin).exporters.append(name)

    def register_hook(self, point: str, fn: Callable[..., Any], priority: int = 100, plugin: str = "builtin") -> None:
        if point not in HOOK_POINTS:
            raise PluginError(f"unknown hook point {point!r}; expected one of {list(HOOK_POINTS)}")
        self.hooks[point].append(HookFn(point=point, fn=fn, priority=priority, plugin=plugin))
        self.hooks[point].sort(key=lambda h: h.priority)
        self._plugin_info(plugin).hooks.append(point)

    def _plugin_info(self, plugin: str) -> PluginInfo:
        if plugin not in self.plugins:
            self.plugins[plugin] = PluginInfo(id=plugin, source="builtin" if plugin == "builtin" else "unknown")
            self._plugin_caps[plugin] = (
                frozenset(Capability) if plugin == "builtin" else frozenset()
            )
        return self.plugins[plugin]

    def declare_plugin(self, info: PluginInfo) -> None:
        self.plugins[info.id] = info
        self._plugin_caps[info.id] = info.capabilities

    def disable_plugin(self, plugin_id: str, reason: str, *, error: str | None = None) -> None:
        info = self.plugins.get(plugin_id)
        if info is None:
            return
        info.disabled = True
        info.violations.append(reason)
        if error:
            info.error = error

    # ── lookups ─────────────────────────────────────────────────────────────

    def capabilities_for(self, plugin: str) -> frozenset[Capability]:
        if plugin == "builtin":
            return frozenset(Capability)
        return self._plugin_caps.get(plugin, frozenset())

    def get_op(self, name: str) -> OpSpec:
        try:
            return self.ops[name]
        except KeyError:
            raise PluginError(
                f"unknown operator {name!r}; registered operators: {', '.join(sorted(self.ops)) or '(none)'}"
            ) from None

    def hooks_for(self, point: str) -> list[HookFn]:
        return list(self.hooks.get(point, ()))

    def formats_matching(self, head: bytes, path: str) -> list[tuple[float, FormatSpec]]:
        """Every format that claims this file, best first."""
        scored: list[tuple[float, FormatSpec]] = []
        for spec in self.formats.values():
            try:
                score = float(spec.match(head, path))
            except Exception:  # noqa: BLE001 - a broken matcher must not break detection
                score = 0.0
            if score > 0:
                scored.append((score, spec))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return scored

    def validate_op_params(self, op: str, params: dict[str, Any]) -> list[str]:
        return validate_params(self.ops[op].params_schema if op in self.ops else None, params)

    def plugin_summary(self) -> list[dict[str, Any]]:
        return [info.as_dict() for info in sorted(self.plugins.values(), key=lambda i: i.id)]

    # ── documentation ───────────────────────────────────────────────────────

    def ops_json(self) -> list[dict[str, Any]]:
        return [self.ops[name].schema_json() for name in sorted(self.ops)]

    def ops_markdown(self) -> str:
        """The generated operator registry.

        This is written to ``skills/pipeline-catalog/op-registry.md`` and read by
        the planner prompt, so the vocabulary the model is offered is always the
        vocabulary the executor actually has.
        """
        lines = [
            "# Operator registry",
            "",
            "Generated by `docpipe ops --markdown`. Do not edit by hand — the planner",
            "reads this list, and a hand edit would describe operators that do not exist.",
            "",
            f"{len(self.ops)} operators, {len(self.formats)} formats, {len(self.pipelines)} pipelines.",
            "",
        ]
        by_class: dict[str, list[OpSpec]] = defaultdict(list)
        for spec in self.ops.values():
            by_class[spec.cost_class].append(spec)

        titles = {
            "free": "Free operators (pure Python, no I/O cost)",
            "local": "Local operators (CPU, filesystem, subprocess — no model calls)",
            "vl": "Vision / model operators (costly, budget-gated)",
        }
        for cost_class in ("free", "local", "vl"):
            specs = sorted(by_class.get(cost_class, []), key=lambda s: s.name)
            if not specs:
                continue
            lines += [f"## {titles[cost_class]}", "", "| op | parameters | requires | summary |", "| --- | --- | --- | --- |"]
            for spec in specs:
                requires = ", ".join(spec.requires) or "-"
                summary = spec.summary.replace("|", "\\|")
                lines.append(f"| `{spec.name}` | `{describe_schema(spec.params_schema)}` | {requires} | {summary} |")
            lines.append("")

        if self.formats:
            lines += ["## Formats", "", "| format | kind | extensions | seekable |", "| --- | --- | --- | --- |"]
            for spec in sorted(self.formats.values(), key=lambda s: s.id):
                exts = ", ".join(spec.extensions) or "-"
                lines.append(f"| `{spec.id}` | {spec.kind} | {exts} | {spec.seekable} |")
            lines.append("")

        if self.pipelines:
            lines += ["## Pipelines", "", "| id | title | steps |", "| --- | --- | --- |"]
            for spec in sorted(self.pipelines.values(), key=lambda s: s.id):
                lines.append(f"| `{spec.id}` | {spec.title} | {len(spec.steps)} |")
            lines.append("")

        if self.plugins:
            lines += ["## Plugins", "", "| id | version | capabilities | contributes |", "| --- | --- | --- | --- |"]
            for info in sorted(self.plugins.values(), key=lambda i: i.id):
                caps = ", ".join(sorted(c.value for c in info.capabilities)) or "-"
                contributes = ", ".join(
                    filter(
                        None,
                        [
                            f"{len(info.ops)} ops" if info.ops else "",
                            f"{len(info.formats)} formats" if info.formats else "",
                            f"{len(info.pipelines)} pipelines" if info.pipelines else "",
                        ],
                    )
                ) or "-"
                lines.append(f"| `{info.id}` | {info.version} | {caps} | {contributes} |")
            lines.append("")
        return "\n".join(lines)


class PluginAPI:
    """The capability-checked facade handed to a plugin's ``register`` function."""

    def __init__(
        self,
        registry: Registry,
        plugin_id: str,
        capabilities: Iterable[Capability | str] = (),
        source: str = "unknown",
    ) -> None:
        self._registry = registry
        self.plugin_id = plugin_id
        self.capabilities: frozenset[Capability] = frozenset(
            c if isinstance(c, Capability) else Capability(c) for c in capabilities
        )
        self._info = PluginInfo(
            id=plugin_id,
            source=source,
            capabilities=self.capabilities,
        )
        registry.declare_plugin(self._info)

    # ── capability helpers ──────────────────────────────────────────────────

    def require(self, capability: Capability) -> None:
        if capability not in self.capabilities:
            raise CapabilityError(
                f"plugin {self.plugin_id!r} requires the {capability.value!r} capability; "
                f"declare it in {PLUGIN_MANIFEST} (declared: {sorted(c.value for c in self.capabilities) or 'none'})"
            )

    @property
    def can_call_models(self) -> bool:
        return Capability.MODEL_CALLS in self.capabilities

    # ── registration surface ────────────────────────────────────────────────

    def register_op(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        summary: str,
        params_schema: dict[str, Any] | None = None,
        cost_class: str = "local",
        requires: Iterable[str] = (),
        produces: Iterable[str] = (),
        backends: Iterable[str] = (),
        unit_kinds: Iterable[str] = (),
    ) -> None:
        """Register an operator. ``summary`` is what the planner reads — write it for
        the model, and say when the operator is the wrong tool."""
        if not summary or len(summary) < 10:
            raise PluginError(f"operator {name!r} needs a summary explaining what it does")
        spec = OpSpec(
            name=name,
            fn=fn,
            summary=summary,
            params_schema=params_schema or {},
            cost_class=cost_class,  # type: ignore[arg-type]
            requires=tuple(requires),
            produces=tuple(produces),
            backends=tuple(backends),
            plugin=self.plugin_id,
            unit_kinds=tuple(unit_kinds),  # type: ignore[arg-type]
        )
        self._registry.register_op(spec)

    def register_format(
        self,
        format_id: str,
        *,
        match: Callable[[bytes, str], float],
        enumerate_units: Callable[..., Iterator[Any]],
        kind: str,
        default_ops: Iterable[str] = (),
        extensions: Iterable[str] = (),
        seekable: bool = True,
        streaming: bool = False,
        max_unit_bytes: int | None = None,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Register a format.

        ``seekable=True`` means unit N can be produced without reading units
        ``< N`` — the promise the runtime shards on. Do not claim it otherwise.

        ``session_factory(path, doc_id, config)`` opens the per-worker handle that
        owns the file and, optionally, rasterises pages. A format without one is
        extractable but cannot use any ``vl_*`` operator.
        """
        spec = FormatSpec(
            id=format_id,
            match=match,
            enumerate_units=enumerate_units,
            kind=kind,  # type: ignore[arg-type]
            default_ops=tuple(default_ops),
            extensions=tuple(extensions),
            seekable=seekable,
            streaming=streaming,
            max_unit_bytes=max_unit_bytes,
            plugin=self.plugin_id,
            session_factory=session_factory,
        )
        self._registry.register_format(spec)

    def register_pipeline(self, spec: PipelineSpec) -> None:
        self._registry.register_pipeline(spec)
        self._info.pipelines.append(spec.id)

    def register_exporter(self, name: str, fn: Callable[..., Any]) -> None:
        self._registry.register_exporter(name, fn, plugin=self.plugin_id)

    def register_hook(self, point: str, fn: Callable[..., Any], priority: int = 100) -> None:
        self._registry.register_hook(point, fn, priority=priority, plugin=self.plugin_id)


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────


def read_manifest(path: Path) -> dict[str, Any]:
    manifest = path / PLUGIN_MANIFEST
    if not manifest.is_file():
        raise PluginError(f"{path} has no {PLUGIN_MANIFEST}")
    with manifest.open("rb") as handle:
        data = tomllib.load(handle)
    for required in ("id", "version", "api_version"):
        if required not in data:
            raise PluginError(f"{manifest} is missing required key {required!r}")
    major = str(data["api_version"]).split(".")[0]
    if major != API_VERSION.split(".")[0]:
        raise PluginError(
            f"{manifest} targets plugin api_version {data['api_version']!r}, "
            f"this runtime implements {API_VERSION!r}"
        )
    return data


def _call_register(registry: Registry, plugin_id: str, manifest: dict[str, Any], register: Callable[..., Any], source: str) -> PluginInfo:
    capabilities = manifest.get("capabilities", [])
    api = PluginAPI(registry, plugin_id, capabilities, source=source)
    api._info.version = str(manifest.get("version", "0"))
    api._info.api_version = str(manifest.get("api_version", API_VERSION))
    api._info.provides = tuple(manifest.get("provides", ()))
    register(api)
    return api._info


def load_path_plugin(registry: Registry, path: Path) -> PluginInfo:
    """Load one directory plugin: manifest + ``module`` (default ``<id>``)."""
    manifest = read_manifest(path)
    plugin_id = str(manifest["id"])
    module_name = str(manifest.get("module", plugin_id.replace("-", "_")))
    source = f"path:{path}"
    register = _resolve_register(path, module_name, plugin_id)
    return _call_register(registry, plugin_id, manifest, register, source)


def _resolve_register(path: Path, module_name: str, plugin_id: str) -> Callable[..., Any]:
    """Find the plugin's ``register`` callable without polluting sys.path globally."""
    package_init = path / module_name / "__init__.py"
    module_file = path / f"{module_name}.py"
    if package_init.is_file() or module_file.is_file():
        sys.path.insert(0, str(path))
        try:
            module = importlib.import_module(module_name)
        finally:
            if sys.path and sys.path[0] == str(path):
                sys.path.pop(0)
    else:
        candidate = package_init if package_init.is_file() else module_file
        if not candidate.is_file():
            raise PluginError(f"{path} has no module {module_name!r} (looked for {package_init.name} or {module_file.name})")
        spec = importlib.util.spec_from_file_location(f"docpipe_plugin_{plugin_id}", candidate)
        if spec is None or spec.loader is None:
            raise PluginError(f"cannot import {candidate}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    register = getattr(module, "register", None)
    if not callable(register):
        raise PluginError(f"{module_name!r} must define a callable `register(api)`")
    return register


def load_entrypoint_plugins(registry: Registry) -> list[PluginInfo]:
    """Load every installed ``docpipe.plugins`` entry point."""
    loaded: list[PluginInfo] = []
    try:
        entry_points = importlib_metadata.entry_points()
        selected = (
            entry_points.select(group=ENTRY_POINT_GROUP)
            if hasattr(entry_points, "select")
            else entry_points.get(ENTRY_POINT_GROUP, [])  # type: ignore[union-attr]
        )
    except Exception:  # noqa: BLE001 - metadata failure must not break a run
        return loaded

    for entry_point in selected:
        plugin_id = entry_point.name
        try:
            register = entry_point.load()
            manifest = {
                "id": plugin_id,
                "version": getattr(entry_point.dist, "version", "0") if entry_point.dist else "0",
                "api_version": API_VERSION,
                "capabilities": [],
            }
            loaded.append(
                _call_register(registry, plugin_id, manifest, register, f"entrypoint:{entry_point.value}")
            )
        except Exception as exc:  # noqa: BLE001 - isolation: one bad plugin is not a broken run
            info = PluginInfo(id=plugin_id, source="entrypoint", disabled=True, error=f"{type(exc).__name__}: {exc}")
            registry.declare_plugin(info)
            loaded.append(info)
    return loaded


def load_plugins(
    registry: Registry,
    plugin_paths: Iterable[str | Path] = (),
    *,
    entry_points: bool = True,
) -> list[PluginInfo]:
    """Load every plugin, isolating failures.

    A plugin that raises during registration is disabled and reported; it never
    takes the run with it. That is the whole point of loading plugins behind a
    manifest and a capability set.
    """
    loaded: list[PluginInfo] = []
    for raw_path in plugin_paths:
        path = Path(raw_path).expanduser()
        try:
            if not path.is_dir():
                raise PluginError(f"plugin path {path} is not a directory")
            loaded.append(load_path_plugin(registry, path))
        except Exception as exc:  # noqa: BLE001
            plugin_id = path.name
            try:
                plugin_id = str(read_manifest(path).get("id", plugin_id))
            except Exception:  # noqa: BLE001
                pass
            info = PluginInfo(id=plugin_id, source=f"path:{path}", disabled=True, error=f"{type(exc).__name__}: {exc}")
            registry.declare_plugin(info)
            loaded.append(info)
    if entry_points:
        loaded.extend(load_entrypoint_plugins(registry))
    return loaded
