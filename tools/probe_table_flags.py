#!/usr/bin/env python3
"""Are the `table_missing` flags content loss, or an evaluator blind spot?

The evaluator counts expected tables by asking pdfplumber for every table it can
find, while the extraction operator refuses to look on pages with more vector
objects than a budget allows. If the flagged pages are the ones the operator
skipped by design, the flag is a false alarm of the same kind as counting a bare
image URI as an unread image: the measurement has to know what the pipeline
deliberately did not attempt.

    tools/rpy.sh --docker tools/probe_table_flags.py <run-dir>
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")


def main(run_dir_str: str) -> int:
    import pyarrow.parquet as pq

    run_dir = Path(run_dir_str)
    evaluation = json.loads((run_dir / "evaluation.json").read_text())
    pages = {row["page_id"]: row for row in pq.read_table(run_dir / "pages.parquet").to_pylist()}

    flagged = []
    for doc in evaluation["documents"]:
        for page in doc.get("pages") or []:
            if any(str(flag).startswith("table_missing") for flag in page.get("flags") or []):
                flagged.append(page["page_id"])

    print(f"{len(flagged)} pages flagged table_missing of {len(pages)} units")
    print("columns available:", sorted(pages[next(iter(pages))].keys()))
    buckets = collections.Counter()
    reasons = collections.Counter()
    for page_id in flagged:
        row = pages.get(page_id)
        if row is None:
            buckets["no record"] += 1
            continue
        provenance = row.get("provenance") or []
        if isinstance(provenance, str):
            provenance = json.loads(provenance)
        ops = [entry.get("op") for entry in provenance if isinstance(entry, dict)]
        if "pdf_tables" not in ops:
            buckets["pdf_tables never ran on this page"] += 1
        else:
            buckets["pdf_tables ran"] += 1
        quality = row.get("quality") or {}
        if isinstance(quality, str):
            quality = json.loads(quality)
        checks = quality.get("checks") or {}
        for key in ("tables_source", "tables_extracted", "table_candidates", "vector_objects"):
            if key in checks:
                reasons[f"{key}={checks[key]}"] += 1
    for name, count in buckets.most_common():
        print(f"  {count:4}  {name}")
    print("  gate metrics:", dict(reasons.most_common(6)))

    # What the block rows say about tables on those pages.
    blocks = pq.read_table(run_dir / "blocks.parquet").to_pylist()
    by_page: dict[str, list[dict]] = collections.defaultdict(list)
    for block in blocks:
        by_page[block["page_id"]].append(block)
    with_table = sum(1 for page_id in flagged if any(b.get("type") == "table" for b in by_page.get(page_id, [])))
    print(f"  {with_table} of the flagged pages did emit a table block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/data/mm-lbfs/out/corpus-keyed2"))
