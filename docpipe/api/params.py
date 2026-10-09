"""A small, strict parameter-schema validator for operators and pipelines.

Deliberately not JSON Schema: the plans the agent writes are validated on a hot
path and must produce *actionable* errors ("dpi must be between 36 and 600, got
6000"), because a validator message is fed straight back to the model in the one
repair round it gets.

Schema shape (per-property, which is what operators actually want to write)::

    {
        "dpi": {"type": "integer", "default": 150, "min": 36, "max": 600},
        "tasks": {"type": "array", "items": {"enum": ["ocr", "tables"]}, "required": True},
        "layout": {"type": "boolean", "default": True},
    }

A top-level ``{"type": "object", "properties": {...}, "required": [...]}`` wrapper
is also accepted, so a plugin that has real JSON Schema can paste it in.
"""

from __future__ import annotations

from typing import Any

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}

#: Values a JSON-ish `enum` may carry, kept permissive on purpose.
_TYPE_NAMES = ", ".join(sorted(_TYPE_CHECKS))


def _unwrap(schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if schema.get("type") == "object" and "properties" in schema:
        return dict(schema["properties"]), list(schema.get("required", []))
    return dict(schema), []


def validate_params(schema: dict[str, Any] | None, params: dict[str, Any] | None) -> list[str]:
    """Return every problem with ``params``. An empty list means valid.

    Unknown parameters are rejected rather than ignored: silently dropping a
    parameter is how a plan that "worked" turns out to have done nothing.
    """
    if not schema:
        return []
    props, top_required = _unwrap(schema)
    params = params or {}
    problems: list[str] = []

    for name in sorted(set(params) - set(props)):
        known = ", ".join(sorted(props)) or "(none)"
        problems.append(f"unknown parameter {name!r}; known parameters: {known}")

    for name, spec in props.items():
        required = bool(spec.get("required", False)) or name in top_required
        if name not in params:
            if required and "default" not in spec:
                problems.append(f"missing required parameter {name!r}")
            continue
        value = params[name]
        problems.extend(_check_value(name, value, spec))
    return problems


def _check_value(name: str, value: Any, spec: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    expected = spec.get("type")

    # bool is a subclass of int in Python; a JSON bool is never an integer.
    if expected == "integer" and isinstance(value, bool):
        return [f"{name} must be an integer, got a boolean"]
    if expected == "number" and isinstance(value, bool):
        return [f"{name} must be a number, got a boolean"]

    if expected in _TYPE_CHECKS and not isinstance(value, _TYPE_CHECKS[expected]):
        problems.append(f"{name} must be {expected}, got {type(value).__name__}")
        return problems
    if expected and expected not in _TYPE_CHECKS:
        problems.append(f"schema error: unknown type {expected!r} (expected one of {_TYPE_NAMES})")
        return problems

    enum = spec.get("enum")
    if enum is not None and value not in enum:
        problems.append(f"{name} must be one of {list(enum)}, got {value!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "min" in spec and value < spec["min"]:
            problems.append(f"{name} must be >= {spec['min']}, got {value}")
        if "max" in spec and value > spec["max"]:
            problems.append(f"{name} must be <= {spec['max']}, got {value}")

    if isinstance(value, str):
        if "minLength" in spec and len(value) < spec["minLength"]:
            problems.append(f"{name} must be at least {spec['minLength']} characters")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            problems.append(f"{name} must be at most {spec['maxLength']} characters")

    items = spec.get("items")
    if items and isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problems.extend(_check_value(f"{name}[{index}]", item, items))
    return problems


def apply_defaults(schema: dict[str, Any] | None, params: dict[str, Any] | None) -> dict[str, Any]:
    """Fill declared defaults. Never invents a value that was not declared."""
    if not schema:
        return dict(params or {})
    props, _ = _unwrap(schema)
    out = dict(params or {})
    for name, spec in props.items():
        if name not in out and "default" in spec:
            default = spec["default"]
            out[name] = list(default) if isinstance(default, list) else default
    return out


def describe_schema(schema: dict[str, Any] | None) -> str:
    """One-line rendering used in prompts and `docpipe ops` output."""
    if not schema:
        return "-"
    props, _ = _unwrap(schema)
    if not props:
        return "-"
    parts: list[str] = []
    for name, spec in sorted(props.items()):
        piece = f"{name}:{spec.get('type', 'any')}"
        if "enum" in spec:
            piece += "[" + "|".join(str(v) for v in spec["enum"]) + "]"
        if "default" in spec:
            piece += f"={spec['default']}"
        if spec.get("required") and "default" not in spec:
            piece = "*" + piece
        parts.append(piece)
    return " ".join(parts)
