#!/usr/bin/env python3
"""What order did a page's blocks end up in, and why?

`content.md` is written by sorting blocks on `order`, so the markdown's reading order
IS the order the operators assigned. This prints that order next to the evidence that
produced it: the column signal, whether the escalation fired, and each block's
horizontal position.

    tools/rpy.sh --docker tools/probe_order.py <run-dir> [pages]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")


def load(run_dir: Path) -> list[dict]:
    import pyarrow.parquet as pq

    return pq.read_table(run_dir / "pages.parquet").to_pylist()


def as_obj(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def main(run_dir_str: str, pages: list[int]) -> int:
    run_dir = Path(run_dir_str)
    rows = load(run_dir)
    content = sorted(run_dir.glob("docs/*/content.md"))
    text = content[0].read_text(encoding="utf-8") if content else ""

    print(f"{run_dir} — {len(rows)} units\n")
    for row in rows:
        page_no = row["page_no"]
        signals = as_obj(row.get("signals"))
        provenance = as_obj(row.get("provenance"))
        ops = [entry.get("op") for entry in provenance if isinstance(entry, dict)]
        orderings = [
            (entry.get("op"), (entry.get("metadata") or {}).get("ordering"))
            for entry in provenance
            if isinstance(entry, dict) and (entry.get("metadata") or {}).get("ordering")
        ]
        print(
            f"p{page_no}: route={row['route']} columns={signals.get('columns')} "
            f"columns_handled={signals.get('columns_handled')} blocks={row.get('n_blocks')}"
        )
        print(f"    ops: {ops}")
        if orderings:
            print(f"    ordering: {orderings}")
        if page_no not in pages:
            continue
        # The first few block texts in the order they were written.
        for line in _page_lines(text, page_no)[:12]:
            print(f"      | {line[:96]}")

    blocks = None
    try:
        import pyarrow.parquet as pq

        blocks = pq.read_table(run_dir / "blocks.parquet").to_pylist()
    except Exception:  # noqa: BLE001
        pass
    if blocks:
        print(f"\nblock geometry for page {pages[0] if pages else 1}:")
        for block in [b for b in blocks if b["page_no"] == (pages[0] if pages else 1)][:14]:
            print(
                f"      order={block['order']:3} x0={block.get('bbox_x0')} x1={block.get('bbox_x1')} "
                f"{(block.get('text') or '')[:46]!r}"
            )
    return 0


def _page_lines(markdown: str, page_no: int) -> list[str]:
    marker = f"<!-- page: {page_no} "
    lines: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if line.startswith("<!-- page:"):
            inside = line.startswith(marker)
            continue
        if inside and line.strip():
            lines.append(line.strip())
    return lines


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], [int(a) for a in sys.argv[2:]] or [1, 2]))
