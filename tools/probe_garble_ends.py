#!/usr/bin/env python3
"""Both ends of the garbling question, measured on text whose answer is known.

    tools/rpy.sh --docker tools/probe_garble_ends.py <garbled.pdf> <chart-page.pdf>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")


def measures(text: str) -> dict[str, float]:
    stripped = text.strip()
    if not stripped:
        return {"all": 1.0, "nonws": 1.0, "replacement": 1.0}
    alnum = sum(1 for char in stripped if char.isalnum())
    nonws = "".join(char for char in stripped if not char.isspace())
    return {
        "all": alnum / len(stripped),
        "nonws": (sum(1 for char in nonws if char.isalnum()) / len(nonws)) if nonws else 1.0,
        "replacement": stripped.count("\ufffd") / len(stripped),
    }


def show(label: str, text: str, pages: int = 3) -> None:
    for page_no in range(1, pages + 1):
        pass
    stats = measures(text)
    sample = " ".join(text.split())[:110]
    print(
        f"  {label:30} all={stats['all']:.3f} nonws={stats['nonws']:.3f} "
        f"repl={stats['replacement']:.4f}  {sample!r}"
    )


def main(paths: list[str]) -> int:
    from docpipe.config import Config
    from docpipe.ingest import pdf as pdfmod

    for path_str in paths:
        path = Path(path_str)
        session = pdfmod.open_session(path, "probe", Config(offline=True))
        try:
            print(f"{path.name}: {session.units()} pages")
            text = "\n".join(session.page_text(page) for page in range(1, min(4, session.units()) + 1))
            show("PDFium/pdftotext text", text)
            # And the assembled reading-order text the evaluator actually judges.
            lines = session.reading_order(1) if hasattr(session, "reading_order") else []
            show("reading order, page 1", " ".join(line.get("text", "") for line in lines))
        finally:
            session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
