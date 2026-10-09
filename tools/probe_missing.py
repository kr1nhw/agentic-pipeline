#!/usr/bin/env python3
"""What exactly is missing from a page that lost text?

`text_lost` says a page's output is missing tokens its own text layer had. That is
a fact; it is not a diagnosis. The missing tokens are: page headers the running-head
rule removed on purpose, table cells that no operator emitted, text the column pass
reordered into a different unit — or content genuinely dropped by a bug.

The multiset difference names them, so the next step is decided by evidence rather
than by guessing which of those it is.

    tools/rpy.sh --docker tools/probe_missing.py <run-dir> [pages]
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")

TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")
CJK = re.compile(r"[\u4e00-\u9fff]")


def tokens(text: str) -> collections.Counter:
    return collections.Counter(match.group(0).lower() for match in TOKEN.finditer(text or "")) + collections.Counter(
        CJK.findall(text or "")
    )


def main(run_dir_str: str, pages: int = 6) -> int:
    import pyarrow.parquet as pq

    run_dir = Path(run_dir_str)
    evaluation = json.loads((run_dir / "evaluation.json").read_text())
    report = json.loads((run_dir / "report.json").read_text())
    names = {doc["doc_id"]: doc["source_path"].split("/")[-1] for doc in report["docs"]}
    rows = {row["page_id"]: row for row in pq.read_table(run_dir / "pages.parquet").to_pylist()}

    flagged = []
    for doc in evaluation["documents"]:
        for page in doc.get("pages") or []:
            if any(str(flag).startswith("text_lost") for flag in page.get("flags") or []):
                flagged.append((page.get("text_recall", 1.0), page["page_id"], doc["source_path"]))
    flagged.sort()
    print(f"{len(flagged)} pages flagged text_lost; the {pages} worst:\n")

    for recall, page_id, source_path in flagged[:pages]:
        row = rows.get(page_id)
        if row is None:
            continue
        source = row.get("text_layer") or ""
        output = row.get("text") or ""
        missing = tokens(source) - tokens(output)
        total_missing = sum(missing.values())
        source_total = sum(tokens(source).values()) or 1
        print(f"== {names.get(row['doc_id'], source_path)} {page_id[-6:]} recall={recall:.2f} "
              f"missing {total_missing}/{source_total} tokens")
        top = ", ".join(f"{token}({count})" for token, count in missing.most_common(14))
        print(f"   most missing: {top}")
        # A missing phrase that repeats is a header; scattered single words are content.
        repeats = sum(count for _, count in missing.most_common(3))
        print(f"   top-3 tokens account for {repeats}/{total_missing} of the loss")

        # Is a missing token gone from the DOCUMENT, or only from this page? Text that
        # moved between units, or was deduplicated against another unit, is not the
        # same defect as text that no longer exists anywhere.
        same_doc = [r for r in rows.values() if r["doc_id"] == row["doc_id"]]
        document_output = tokens("\n".join((r.get("text") or "") for r in same_doc))
        gone = {token: count for token, count in missing.items() if document_output.get(token, 0) == 0}
        moved = total_missing - sum(gone.values())
        print(f"   {moved} token(s) still present elsewhere in the document, {sum(gone.values())} gone from it entirely")
        if gone:
            print("   gone from the document:", ", ".join(f"{t}({c})" for t, c in sorted(gone.items(), key=lambda kv: -kv[1])[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], *[int(a) for a in sys.argv[2:]]))
