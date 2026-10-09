"""Assemble the default registry: formats, operators, pipelines, plugins.

Builtins register through the public :class:`PluginAPI` with the full capability
set, and third-party plugins register through the same facade with only what they
declared. One code path, two privilege levels — which is the only way an
extension interface stays honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from docpipe import API_VERSION
from docpipe.api.registry import PluginAPI, Registry, load_plugins
from docpipe.api.types import Capability, PluginError
from docpipe.ingest import register_formats
from docpipe.ops import register_ops
from docpipe.router import validate_rule
from docpipe.schema import PipelineSpec

PIPELINE_DIR = Path(__file__).parent / "pipelines"
PIPELINE_SUFFIXES = (".pipeline.yml", ".pipeline.yaml")


class CatalogError(PluginError):
    """The static pipeline catalog is invalid. Fatal: shipping a bad catalog is
    shipping a wrong default, and a wrong default is invisible."""


def load_pipeline_file(path: Path) -> PipelineSpec:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"{path.name}: expected a mapping at the top level")
    spec = PipelineSpec.model_validate(raw)
    if spec.api_version.split(".")[0] != API_VERSION.split(".")[0]:
        raise CatalogError(
            f"{path.name}: pipeline api_version {spec.api_version!r} does not match runtime {API_VERSION!r}"
        )
    problems = validate_rule(spec.match)
    if problems:
        raise CatalogError(f"{path.name}: " + "; ".join(problems))
    if not spec.steps:
        raise CatalogError(f"{path.name}: a pipeline with no steps extracts nothing")
    return spec


def load_pipelines(directory: Path = PIPELINE_DIR) -> list[PipelineSpec]:
    specs: list[PipelineSpec] = []
    if not directory.is_dir():
        return specs
    for path in sorted(directory.iterdir()):
        if path.suffixes and any(path.name.endswith(suffix) for suffix in PIPELINE_SUFFIXES):
            specs.append(load_pipeline_file(path))
    return specs


def validate_catalog(registry: Registry) -> list[str]:
    """Check every pipeline against the live operator registry.

    This runs at startup rather than at first use: a catalog entry naming an
    operator that does not exist would otherwise fail silently on the one document
    that chose it.
    """
    problems: list[str] = []
    for spec in registry.pipelines.values():
        for step in spec.steps:
            if step.op not in registry.ops:
                problems.append(f"{spec.id}: unknown operator {step.op!r}")
                continue
            op_spec = registry.ops[step.op]
            schema = op_spec.params_schema
            if schema:
                from docpipe.api.params import validate_params

                for problem in validate_params(schema, step.params):
                    problems.append(f"{spec.id}/{step.op}: {problem}")
        if spec.verify.gate not in registry.ops:
            problems.append(f"{spec.id}: unknown gate {spec.verify.gate!r}")
        if spec.verify.escalate_to and spec.verify.escalate_to not in registry.ops:
            problems.append(f"{spec.id}: unknown escalation operator {spec.verify.escalate_to!r}")
    return problems


def build_registry(
    *,
    plugin_paths: Iterable[str | Path] = (),
    load_entrypoints: bool = True,
    pipeline_dir: Path = PIPELINE_DIR,
    strict: bool = True,
) -> Registry:
    """Build the registry a run uses."""
    registry = Registry()
    api = PluginAPI(registry, "builtin", capabilities=frozenset(Capability), source="builtin")
    register_formats(api)
    register_ops(api)
    for spec in load_pipelines(pipeline_dir):
        api.register_pipeline(spec)

    # Plugins load after builtins so they can extend or override intent, and
    # after the catalog so a pipeline can reference a plugin operator.
    loaded = load_plugins(registry, plugin_paths, entry_points=load_entrypoints)
    failed = [info for info in loaded if info.disabled]
    for info in failed:
        registry.disable_plugin(info.id, f"load failed: {info.error}", error=info.error)

    problems = validate_catalog(registry)
    if problems and strict:
        raise CatalogError("; ".join(problems[:10]))
    return registry


def catalog_summary(registry: Registry) -> dict[str, Any]:
    return {
        "ops": len(registry.ops),
        "formats": len(registry.formats),
        "pipelines": sorted(registry.pipelines),
        "plugins": registry.plugin_summary(),
    }
