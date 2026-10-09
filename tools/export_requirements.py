#!/usr/bin/env python3
"""Emit requirements.txt from pyproject.toml.

Kept next to the build script rather than in the package: it is build tooling, and
it exists so the Dockerfile can install dependencies in a cacheable layer without
duplicating the dependency list.
"""

from __future__ import annotations

import pathlib
import sys
import tomllib


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1] if len(argv) > 1 else ".")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lines = list(project.get("dependencies", []))
    lines += list(project.get("optional-dependencies", {}).get("dev", []))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
