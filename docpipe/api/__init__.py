"""The plugin API (v1) — the extension standard.

Everything a plugin may do is reachable from here, and it is the same surface the
builtins use:

    from docpipe.api import registry, OpResult, Capability

    def register(api: registry.PluginAPI) -> None:
        api.register_op("my_op", my_fn, summary="…", cost_class="local")

See ``docs/plugin-spec.md`` for the manifest, the capability model, and the
guarantees (and non-guarantees) of each capability.
"""

from docpipe.api.params import apply_defaults, describe_schema, validate_params
from docpipe.api.registry import (
    ENTRY_POINT_GROUP,
    HOOK_POINTS,
    PLUGIN_MANIFEST,
    PluginAPI,
    PluginInfo,
    Registry,
    load_path_plugin,
    load_plugins,
    read_manifest,
)
from docpipe.api.types import (
    Capability,
    CapabilityError,
    Cost,
    FormatSpec,
    OpContext,
    OpError,
    OpResult,
    OpSpec,
    PageState,
    PluginError,
    UnitSeed,
)

__all__ = [
    "Capability", "CapabilityError", "Cost", "ENTRY_POINT_GROUP", "FormatSpec", "HOOK_POINTS",
    "OpContext", "OpError", "OpResult", "OpSpec", "PLUGIN_MANIFEST", "PageState", "PluginAPI",
    "PluginError", "PluginInfo", "Registry", "UnitSeed", "apply_defaults", "describe_schema",
    "load_path_plugin", "load_plugins", "read_manifest", "registry", "validate_params",
]
