#!/usr/bin/env python3
"""Why did these units end up in needs_review?

Reads a run's `pages.parquet` and prints, for every unit the run could not
resolve, the gate reasons and the checks that produced them. A unit in
needs_review is the pipeline admitting it does not know; the point of looking is
to decide whether the gate is right (the document really is unreadable) or the
rule is wrong (the document is fine and the check misfired).
"""

from __future__ import annotations

import glob
import json
import os
import sys

import pyarrow.parquet as pq


def as_obj(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def main(run_dir: str, only: str | None = None) -> int:
    table = pq.read_table(os.path.join(run_dir, "pages.parquet"))
    rows = table.to_pylist()
    print(f"{run_dir}: {len(rows)} units, columns {len(table.column_names)}")
    shown = 0
    for row in rows:
        route = row.get("route")
        page = str(row.get("page_id") or "")
        if route != "needs_review" or (only and only not in page):
            continue
        shown += 1
        quality = as_obj(row.get("quality")) or {}
        blocks = as_obj(row.get("blocks")) or []
        signals = as_obj(row.get("signals")) or {}
        print("\n== " + page)
        print("   kind          ", row.get("kind"))
        print("   reasons       ", quality.get("reasons"))
        checks = quality.get("checks") or {}
        interesting = {
            key: value
            for key, value in checks.items()
            if value not in (None, False, True) or key.endswith(("_ok", "trust"))
        }
        print("   checks        ", json.dumps(interesting, default=str)[:400])
        print("   text chars    ", row.get("text_layer_chars"), " blocks", len(blocks) if isinstance(blocks, list) else "?")
        if isinstance(signals, dict):
            keep = {k: signals.get(k) for k in ("columns", "text_layer_trust", "n_paths", "median_font", "chars", "words") if k in signals}
            print("   signals       ", json.dumps(keep, default=str)[:300])
        text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        print("   extracted     ", text[:220].replace("\n", " ") or "(nothing)")
    if not shown:
        print("no units in needs_review")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "/data/mm-lbfs/out/corpus-keyed2"
    needle = sys.argv[2] if len(sys.argv) > 2 else None
    raise SystemExit(main(target, needle))
