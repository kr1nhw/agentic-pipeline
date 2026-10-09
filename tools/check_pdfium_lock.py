#!/usr/bin/env python3
"""Every PDFium call must be inside `with _PDFIUM_LOCK`.

PDFium keeps process-global state and is not thread-safe. The run fans pages out
over a thread pool, so one unlocked call is enough to segfault the whole process —
which is exactly what happened: exit 139 with no Python traceback, taking the run's
artifacts with it, on a 357-page document that had processed fifteen documents
before it.

Eyeballing twenty call sites does not work; this walks the AST and reports any
call whose expression mentions a PDFium API and whose enclosing `with` blocks do
not include the lock.

    python tools/check_pdfium_lock.py [path]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: Names that reach PDFium, directly or through a session attribute.
PDFIUM_MARKERS = (
    "pdfium",
    "_pdfium",
    "_raw",
    "_pdf.",
    "self._pdf[",
)


def enclosing_locks(tree: ast.AST) -> dict[ast.AST, set[str]]:
    """Map every node to the set of lock names of its enclosing `with` blocks."""
    locks: dict[ast.AST, set[str]] = {}

    def visit(node: ast.AST, active: set[str]) -> None:
        locks[node] = active
        for child in ast.iter_child_nodes(node):
            extra = set(active)
            if isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    text = ast.unparse(item.context_expr)
                    if "LOCK" in text or "lock" in text:
                        extra.add(text)
            visit(child, extra)

    visit(tree, set())
    return locks


def unlocked_subscripts(tree: ast.AST, locks: dict[ast.AST, set[str]], source: str) -> list[tuple[int, str]]:
    """`self._pdf[3]` is a PDFium call that looks like an index.

    It is `FPDF_LoadPage` through `__getitem__`, so it takes no parentheses and a
    checker that only walks Call nodes never sees it. One such line sat directly
    above a `with _PDFIUM_LOCK:` block on the column-escalation path and was the
    only unlocked PDFium access in the file.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        text = ast.unparse(node)
        if "_pdf[" not in text and "_pdfium" not in text:
            continue
        if locks.get(node):
            continue
        line = source.splitlines()[node.lineno - 1].strip()
        found.append((node.lineno, line))
    return found


def main(path_str: str) -> int:
    path = Path(path_str)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    locks = enclosing_locks(tree)

    offenders: list[tuple[int, str, str]] = []
    locked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        text = ast.unparse(node.func)
        if not any(marker in text for marker in PDFIUM_MARKERS):
            continue
        # `self._pdfium_page_text(n)` is this module's own wrapper, checked where
        # it is defined. Flagging its call sites would bury the real finding.
        if text.rsplit(".", 1)[-1].startswith("_pdfium_"):
            continue
        active = locks.get(node, set())
        if active:
            locked += 1
            continue
        offenders.append((node.lineno, text, lines[node.lineno - 1].strip()))

    subscripts = unlocked_subscripts(tree, locks, source)
    for lineno, line in subscripts:
        offenders.append((lineno, "PDFium page/document index (__getitem__)", line))

    print(f"{path}: {locked} locked PDFium calls, {len(offenders)} UNLOCKED")
    for lineno, call, line in sorted(offenders):
        print(f"  line {lineno:5}: {call}   ->   {line[:90]}")
    return 1 if offenders else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "docpipe/ingest/pdf.py"))
