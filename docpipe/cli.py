#!/usr/bin/env python3
"""docpipe command line interface.

Commands are deliberately few and each one does one thing:

    docpipe run        extract content from files (the product)
    docpipe probe      report what the router sees, without extracting
    docpipe ops        the operator registry, including the generated markdown
    docpipe pipelines  the static catalog
    docpipe plugin     installed extensions and their capabilities
    docpipe selftest   prove this image is self-consistent
    docpipe env        a JSON capability report for this runtime
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from docpipe import API_VERSION, __version__
from docpipe.builtins import CatalogError, build_registry, catalog_summary
from docpipe.config import Config

REQUIRED_MODULES = (
    "pypdfium2", "pdfplumber", "pptx", "docx", "PIL", "pyarrow", "pandas", "httpx", "pydantic", "yaml",
)
OPTIONAL_MODULES = ("daft",)
OPTIONAL_BINARIES = ("pdftotext", "pdftoppm", "pdfinfo", "gs", "tesseract", "ffmpeg", "sqlite3", "node", "dsh")


# ─────────────────────────────────────────────────────────────────────────────
# Reporting helpers
# ─────────────────────────────────────────────────────────────────────────────


def _probe_modules(names: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
            found[name] = f"ERROR: {type(exc).__name__}: {exc}"
        else:
            found[name] = str(getattr(module, "__version__", "ok"))
    return found


def env_report() -> dict[str, Any]:
    return {
        "docpipe_version": __version__,
        "plugin_api_version": API_VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "pid1_user": os.getuid(),
        "cwd": os.getcwd(),
        "env": {
            key: os.environ.get(key)
            for key in ("DSH_HOME", "DOCPIPE_HOME", "DOCPIPE_WORK", "DOCPIPE_OFFLINE", "DOCPIPE_MODEL", "DOCPIPE_BASE_URL")
            if os.environ.get(key) is not None
        },
        "api_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "required_modules": _probe_modules(REQUIRED_MODULES),
        "optional_modules": _probe_modules(OPTIONAL_MODULES),
        "optional_binaries": {name: (shutil.which(name) or None) for name in OPTIONAL_BINARIES},
    }


def selftest() -> int:
    report = env_report()
    failures: list[str] = []
    for name, status in report["required_modules"].items():
        if status.startswith("ERROR"):
            failures.append(f"python module {name}: {status}")
    try:
        import pypdfium2  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        failures.append(f"pypdfium2 unusable: {exc}")

    warnings: list[str] = []
    if not shutil.which("pdftotext"):
        warnings.append("pdftotext missing: the text layer will use the PDFium backend")

    # The registry must build and its catalog must validate. A broken catalog is
    # a wrong default, which is worse than a crash.
    try:
        registry = build_registry(load_entrypoints=False)
        report["catalog"] = catalog_summary(registry)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"registry/catalog invalid: {type(exc).__name__}: {exc}")

    report["failures"] = failures
    report["warnings"] = warnings
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    if failures:
        print(f"\nSELFTEST FAILED ({len(failures)} problem(s))", file=sys.stderr)
        return 1
    print("\nSELFTEST OK", file=sys.stderr)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    from docpipe.agent.vl import Budget, VLClient
    from docpipe.exec.runner import Runner

    config = _config_from_args(args)
    if args.offline:
        config.offline = True
    if args.deterministic:
        config.deterministic_only = True

    registry = build_registry(
        plugin_paths=config.plugin_paths,
        load_entrypoints=config.load_entrypoint_plugins,
    )

    vl = None
    budget = Budget(config)
    if config.model_enabled:
        vl = VLClient(config, budget=budget, log=_structured_log)
        if not vl.configured:
            print(
                "docpipe: no DEEPSEEK_API_KEY in the environment — continuing with deterministic "
                "operators only (this is equivalent to --offline)",
                file=sys.stderr,
            )
            vl = None

    runner = Runner(config, registry, vl=vl, budget=budget, log=_structured_log)
    try:
        report = runner.run([Path(item) for item in args.inputs])
    finally:
        if vl is not None:
            vl.close()

    _print_run_summary(report, config)
    return 0 if not report.stopped_reason else 3


def cmd_probe(args: argparse.Namespace) -> int:
    """Report route decisions and signals without extracting anything."""
    from docpipe.exec.runner import Runner

    config = _config_from_args(args)
    config.offline = True
    registry = build_registry(plugin_paths=config.plugin_paths)
    runner = Runner(config, registry, log=_structured_log)
    rows: list[dict[str, Any]] = []
    for path in runner.discover([Path(item) for item in args.inputs]):
        try:
            job = runner._prepare(path)  # noqa: SLF001 - probe is the diagnostic view of preparation
        except Exception as exc:  # noqa: BLE001
            rows.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        if job is None:
            rows.append({"path": str(path), "error": "unknown format"})
            continue
        rows.append(
            {
                "path": str(path),
                "doc_id": job.doc_id,
                "format": job.format_id,
                "units": job.units,
                "pipeline": job.decision.pipeline_id,
                "confidence": round(job.decision.confidence, 3),
                "reason": job.decision.reason,
                "candidates": [{"id": cid, "score": round(score, 3)} for cid, score in job.decision.candidates],
                "signals": {
                    key: (round(value, 4) if isinstance(value, float) else value)
                    for key, value in job.signals.items()
                },
            }
        )
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    if args.summary:
        static = sum(1 for row in rows if row.get("pipeline"))
        print(
            f"\n{static}/{len(rows)} document(s) have a static pipeline; "
            f"{len(rows) - static} would need the dynamic planner",
            file=sys.stderr,
        )
    return 0


def cmd_ops(args: argparse.Namespace) -> int:
    registry = build_registry(load_entrypoints=args.with_plugins)
    if args.markdown:
        text = registry.ops_markdown()
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}", file=sys.stderr)
        else:
            print(text)
        return 0
    print(json.dumps(registry.ops_json(), indent=2, ensure_ascii=False))
    return 0


def cmd_pipelines(args: argparse.Namespace) -> int:
    registry = build_registry(load_entrypoints=False)
    if args.action == "list":
        for spec in sorted(registry.pipelines.values(), key=lambda s: s.id):
            print(f"{spec.id:28} v{spec.version:4} {len(spec.steps):2} step(s)  {spec.title}")
        return 0
    if args.action == "show":
        spec = registry.pipelines.get(args.id)
        if spec is None:
            print(f"no pipeline {args.id!r}", file=sys.stderr)
            return 1
        print(json.dumps(spec.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0
    if args.action == "validate":
        from docpipe.builtins import validate_catalog

        problems = validate_catalog(registry)
        if problems:
            for problem in problems:
                print(f"INVALID: {problem}", file=sys.stderr)
            return 1
        print(f"catalog OK: {len(registry.pipelines)} pipeline(s), {len(registry.ops)} operator(s)")
        return 0
    print(f"unknown action {args.action!r}", file=sys.stderr)
    return 2


def cmd_plugin(args: argparse.Namespace) -> int:
    from docpipe.builtins import validate_catalog

    if args.action == "list":
        registry = build_registry(load_entrypoints=True)
        print(json.dumps(registry.plugin_summary(), indent=2, ensure_ascii=False))
        return 0
    if args.action == "lint":
        target = Path(args.target)
        from docpipe.api.registry import load_path_plugin
        from docpipe.api.registry import Registry

        registry = Registry()
        try:
            info = load_path_plugin(registry, target)
        except Exception as exc:  # noqa: BLE001
            print(f"LINT FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        problems = validate_catalog(registry)
        print(json.dumps({"plugin": info.as_dict(), "catalog_problems": problems}, indent=2, ensure_ascii=False))
        return 1 if problems else 0
    print(f"unknown action {args.action!r}", file=sys.stderr)
    return 2


# ─────────────────────────────────────────────────────────────────────────────
# Shared
# ─────────────────────────────────────────────────────────────────────────────


def _structured_log(event: str, **fields: Any) -> None:
    if os.environ.get("DOCPIPE_LOG_JSON", "1") != "0":
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), file=sys.stderr)


def _config_from_args(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    for field, attribute in (
        ("out_dir", "out"),
        ("work_dir", "work"),
        ("model", "model"),
        ("engine", "engine"),
        ("shard", "shard"),
        ("workers", "workers"),
        ("vl_workers", "vl_workers"),
        ("plugin_paths", "plugin"),
    ):
        value = getattr(args, attribute, None)
        if value:
            overrides[field] = value
    for field in ("max_vl_calls_per_run", "max_tokens_per_run", "max_usd", "max_vl_calls_per_doc"):
        value = getattr(args, field, None)
        if value:
            overrides[field] = value
    if getattr(args, "keep_rasters", False):
        overrides["keep_rasters"] = True
    if getattr(args, "resume", False):
        overrides["resume"] = True
    if getattr(args, "no_cache", False):
        overrides["no_cache"] = True
    return Config(**overrides)


def _print_run_summary(report: Any, config: Config) -> None:
    totals = report.totals
    print("\n=== docpipe run ===", file=sys.stderr)
    print(f"documents        {totals.get('documents')}", file=sys.stderr)
    print(f"units            {totals.get('units')}", file=sys.stderr)
    routes = totals.get("route_counts") or {}
    for route, count in sorted(routes.items()):
        print(f"  route {route:14} {count}", file=sys.stderr)
    print(f"gate failures    {totals.get('gate_failures')}", file=sys.stderr)
    print(f"needs review     {totals.get('needs_review')}", file=sys.stderr)
    print(f"vision calls     {totals.get('vl_calls')}", file=sys.stderr)
    print(f"tokens           prompt {totals.get('prompt_tokens')} / completion {totals.get('completion_tokens')}", file=sys.stderr)
    if config.price_prompt_per_mtok or config.price_completion_per_mtok:
        print(f"cost estimate    ${totals.get('usd_est')}", file=sys.stderr)
    cache = totals.get("cache") or {}
    print(f"cache            hits {cache.get('hits', 0)} / misses {cache.get('misses', 0)}", file=sys.stderr)
    print(f"artifacts        {config.out_dir}", file=sys.stderr)
    if report.stopped_reason:
        print(f"STOPPED          {report.stopped_reason} (re-run with --resume to continue)", file=sys.stderr)
    failed = [doc for doc in report.docs if doc.errors]
    if failed:
        print(f"{len(failed)} document(s) reported errors; see {config.out_dir / 'report.json'}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docpipe", description="Agentic pipeline for extracting content from documents.")
    parser.add_argument("--version", action="version", version=f"docpipe {__version__} (plugin api {API_VERSION})")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("env", help="print a JSON capability report for this runtime")
    sub.add_parser("selftest", help="verify the image is self-consistent (exit 1 on failure)")

    run = sub.add_parser("run", help="extract content from files or directories")
    run.add_argument("--in", dest="inputs", nargs="+", required=True, help="files or directories to process")
    run.add_argument("--out", default=os.environ.get("DOCPIPE_OUT", "/out"))
    run.add_argument("--work", default=os.environ.get("DOCPIPE_WORK", "/work"))
    run.add_argument("--model", default=None, help="model id for the vision route")
    run.add_argument("--offline", action="store_true", help="disable every model call")
    run.add_argument("--deterministic", action="store_true", help="static pipelines only")
    run.add_argument("--engine", choices=["local", "daft", "auto"], default="local")
    run.add_argument("--workers", type=int, default=0, help="local-operator workers (0 = half the CPUs)")
    run.add_argument("--vl-workers", dest="vl_workers", type=int, default=0)
    run.add_argument("--shard", default=None, help="'i/N' deterministic work partition")
    run.add_argument("--plugin", action="append", default=None, help="plugin directory (repeatable)")
    run.add_argument("--keep-rasters", dest="keep_rasters", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--no-cache", dest="no_cache", action="store_true")
    run.add_argument("--max-vl-calls-per-run", dest="max_vl_calls_per_run", type=int, default=0)
    run.add_argument("--max-vl-calls-per-doc", dest="max_vl_calls_per_doc", type=int, default=0)
    run.add_argument("--max-tokens-per-run", dest="max_tokens_per_run", type=int, default=0)
    run.add_argument("--max-usd", dest="max_usd", type=float, default=0.0)

    probe = sub.add_parser("probe", help="report routing decisions without extracting")
    probe.add_argument("--in", dest="inputs", nargs="+", required=True)
    probe.add_argument("--out", default=os.environ.get("DOCPIPE_OUT", "/out"))
    probe.add_argument("--work", default=os.environ.get("DOCPIPE_WORK", "/work"))
    probe.add_argument("--plugin", action="append", default=None)
    probe.add_argument("--summary", action="store_true")

    ops = sub.add_parser("ops", help="list the operator registry")
    ops.add_argument("--markdown", action="store_true", help="emit the generated registry markdown")
    ops.add_argument("--output", default=None, help="write to a file instead of stdout")
    ops.add_argument("--with-plugins", dest="with_plugins", action="store_true")

    pipelines = sub.add_parser("pipelines", help="inspect the static pipeline catalog")
    pipelines.add_argument("action", choices=["list", "show", "validate"], nargs="?", default="list")
    pipelines.add_argument("id", nargs="?", default=None)

    plugin = sub.add_parser("plugin", help="inspect or validate extensions")
    plugin.add_argument("action", choices=["list", "lint"], nargs="?", default="list")
    plugin.add_argument("target", nargs="?", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    handlers = {
        "env": lambda a: (print(json.dumps(env_report(), indent=2, ensure_ascii=False, sort_keys=True)) or 0),
        "selftest": lambda a: selftest(),
        "run": cmd_run,
        "probe": cmd_probe,
        "ops": cmd_ops,
        "pipelines": cmd_pipelines,
        "plugin": cmd_plugin,
    }
    try:
        return handlers[args.command](args)
    except CatalogError as exc:
        print(f"docpipe: pipeline catalog is invalid: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
