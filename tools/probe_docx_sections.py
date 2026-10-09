#!/usr/bin/env python3
"""What the DOCX reader sees, next to what the run emitted for the same unit.

The evaluator compares the reader's own text layer against the assembled blocks,
so a gap between them is not a judgement call: the same session reported the text
and the operators failed to emit it. This prints both sides per section.

    tools/rpy.sh --docker tools/probe_docx_sections.py <file.docx>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")


def main(path_str: str) -> int:
    from docpipe.config import Config
    from docpipe.ingest import office

    path = Path(path_str)
    config = Config(offline=True)
    session = office.open_docx(path, "probe", config)
    sections = session.sections()
    print(f"{path.name}: {len(sections)} sections")
    for index, section in enumerate(sections, start=1):
        text = session.section_text(index)
        table_chars = sum(len(cell) for table in section["tables"] for row in table for cell in row)
        paragraphs = section["paragraphs"]
        styles = {}
        for paragraph in paragraphs:
            styles[paragraph.get("style") or "(none)"] = styles.get(paragraph.get("style") or "(none)", 0) + 1
        print(
            f"  section {index}: chars={len(text):6} paragraphs={len(paragraphs):3} "
            f"tables={len(section['tables']):2} table_chars={table_chars:5} recovered={bool(section.get('recovered'))}"
        )
        print(f"    styles: {json.dumps(styles)}")
        print(f"    first 120: {text[:120]!r}")
        # What the paragraph operator would emit, by its own rule.
        emitted = len(section["heading"] or "") + sum(len(p["text"]) for p in paragraphs)
        print(f"    operator would emit ~{emitted} chars; tables carry {table_chars}")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
