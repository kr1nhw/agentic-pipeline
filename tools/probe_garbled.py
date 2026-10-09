#!/usr/bin/env python3
"""Are the `garbled` flags real, or a heuristic that cannot read mathematics?

The evaluator flags a page as garbled when the extracted text does not look like
prose: too few alphanumeric characters, too many replacement characters, runs of
symbols. On a corpus of mathematics papers that is a claim worth checking rather
than a verdict — a page of formulas is legitimately symbol-dense, and a rule tuned
on prose will call it broken.

    tools/rpy.sh --docker tools/probe_garbled.py <run-dir> [samples]
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")


def ratios(text: str) -> dict[str, float]:
    stripped = text.strip()
    if not stripped:
        return {"chars": 0, "alnum": 0.0, "replacement": 0.0, "non_ascii": 0.0, "word_shape": 0.0}
    letters = sum(1 for ch in stripped if ch.isalnum())
    tokens = [token for token in stripped.split() if any(ch.isalnum() for ch in token)]
    wordish = sum(
        1
        for token in tokens
        if len(token.strip(".,;:!?()[]{}<>\"'`/*_+=|\\")) >= 2
        and all(ch.isalpha() or ch in "-'" for ch in token.strip(".,;:!?()[]{}<>\"'`/*_+=|\\"))
    )
    return {
        "chars": len(stripped),
        "alnum": letters / len(stripped),
        "replacement": stripped.count("\ufffd") / len(stripped),
        "non_ascii": sum(1 for ch in stripped if ord(ch) > 127) / len(stripped),
        "word_shape": wordish / len(tokens) if tokens else 0.0,
    }


def main(run_dir_str: str, samples: int = 6) -> int:
    import pyarrow.parquet as pq

    run_dir = Path(run_dir_str)
    evaluation = json.loads((run_dir / "evaluation.json").read_text())
    pages = {row["page_id"]: row for row in pq.read_table(run_dir / "pages.parquet").to_pylist()}

    flagged: list[tuple[str, str]] = []
    counts = collections.Counter()
    for doc in evaluation["documents"]:
        for page in doc.get("pages") or []:
            for flag in page.get("flags") or []:
                if str(flag).startswith("garbled"):
                    flagged.append((page["page_id"], str(flag)))
                    counts[str(flag)[:60]] += 1

    print(f"{len(flagged)} pages flagged garbled of {len(pages)} units")
    for flag, count in counts.most_common(5):
        print(f"  {count:4}  {flag}")

    # Is the flag concentrated in particular kinds of document?
    by_format = collections.Counter()
    for page_id, _ in flagged:
        row = pages.get(page_id)
        if row:
            by_format[row.get("kind", "?")] += 1
    print("  by unit kind:", dict(by_format))

    print(f"\n  {'page':22} {'chars':>7} {'alnum':>6} {'repl':>6} {'nonascii':>8} {'wordish':>7}  sample")
    for page_id, _ in flagged[:samples]:
        row = pages.get(page_id) or {}
        text = row.get("text") or row.get("text_layer") or ""
        stats = ratios(text)
        sample = re.sub(r"\s+", " ", text.strip())[:70]
        print(
            f"  {page_id[:22]:22} {stats['chars']:7} {stats['alnum']:6.2f} {stats['replacement']:6.3f} "
            f"{stats['non_ascii']:8.2f} {stats['word_shape']:7.2f}  {sample!r}"
        )

    # For contrast, pages that were NOT flagged.
    clean = [(pid, row) for pid, row in pages.items() if pid not in {p for p, _ in flagged}]
    print("\n  unflagged pages, for contrast:")
    for page_id, row in clean[:3]:
        stats = ratios(row.get("text") or "")
        print(f"  {page_id[:22]:22} {stats['chars']:7} {stats['alnum']:6.2f} {stats['word_shape']:7.2f}")
    return 0


if __name__ == "__main__":
    arguments = [int(a) for a in sys.argv[2:]]
    raise SystemExit(main(sys.argv[1], *arguments))
