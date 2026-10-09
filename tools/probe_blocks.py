#!/usr/bin/env python3
"""Compare one document's emitted blocks against its reader text, item by item.

    tools/rpy.sh --docker tools/probe_blocks.py /data/mm-lbfs/out/office2 endnotes.docx
"""

from __future__ import annotations

import json
import sys

import pyarrow.parquet as pq


def main(run_dir: str, needle: str) -> int:
    pages = pq.read_table(f"{run_dir}/pages.parquet").to_pylist()
    blocks = pq.read_table(f"{run_dir}/blocks.parquet").to_pylist()
    # doc_ids are content hashes, so the file name has to be resolved through the
    # run's own report before any of its units can be selected.
    matched: set[str] = set()
    try:
        with open(f"{run_dir}/report.json", encoding="utf-8") as handle:
            report = json.load(handle)
        for doc in report.get("docs", []):
            if needle.lower() in doc.get("source_path", "").lower():
                matched.add(doc["doc_id"])
    except FileNotFoundError:
        pass
    print(f"{len(pages)} units, {len(blocks)} blocks; matched docs {sorted(matched)}")
    for page in pages:
        if page.get("doc_id") not in matched:
            continue
        own = [b for b in blocks if b.get("page_id") == page.get("page_id")]
        text = " ".join(b.get("text") or "" for b in own)
        print(f"\n== {page['page_id']} kind={page['kind']} route={page['route']} blocks={len(own)} block_chars={len(text)}")
        print(f"   text_layer_chars={page.get('text_layer_chars')} output_chars={len(text.strip())}")
        print(f"   ops: {sorted({b.get('source_op') for b in own})}")
        for block in sorted(own, key=lambda b: b.get("order") or 0)[:6]:
            body = (block.get("text") or "").replace("\n", " ")
            print(f"     [{block.get('order')}] {block.get('type'):10} {block.get('source_op'):24} {len(block.get('text') or ''):5}  {body[:70]!r}")
        if len(own) > 6:
            print(f"     ... {len(own) - 6} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
