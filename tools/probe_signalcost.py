#!/usr/bin/env python3
"""What does one page of signalling actually cost, and where does it go?

The probe must decide a document's archetype before extraction, so it reads pages.
The question this answers is what "reading a page" costs, split into its parts, so
the cost of the pages nobody asked about can be stated rather than guessed:

    page_size      PDFium geometry
    page_text      pdftotext for a 32-page window (shared by 32 pages)
    page_layout    PDFium object walk: paths, rules, median font size
    text_layer_trust  font table (cached per session) + word shape when fonts doubt

    tools/rpy.sh --docker tools/probe_signalcost.py <pdf> [pages]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")


def timed(label: str, fn, pages: int) -> float:
    """Run one pass over ``pages`` pages and report the per-page cost.

    `pages` is how many pages the callback covers, not a repeat count: passing it
    as a repeat count and dividing by it made every figure here report the cost of
    the whole pass as if it were one page, which overstated the projection by 8x
    and sent this probe chasing a cost that did not exist.
    """
    started = time.monotonic()
    fn()
    elapsed = (time.monotonic() - started) / pages
    print(f"  {label:24} {elapsed * 1000:8.1f} ms/page")
    return elapsed


def main(path_str: str, pages: int = 8) -> int:
    from docpipe.config import Config
    from docpipe.ingest import pdf as pdfmod

    path = Path(path_str)
    session = pdfmod.open_session(path, "probe", Config(offline=True))
    try:
        units = session.units()
        print(f"{path.name}: {units} pages, {path.stat().st_size / 1024 / 1024:.1f} MiB")

        timings = {}
        timings["size"] = timed("page_size", lambda: [session.page_size(n) for n in range(1, pages + 1)], pages)
        timings["text"] = timed("page_text", lambda: [session.page_text(n) for n in range(1, pages + 1)], pages)
        timings["layout"] = timed("page_layout", lambda: [session.page_layout(n) for n in range(1, pages + 1)], pages)
        timings["trust"] = timed(
            "text_layer_trust", lambda: [session.text_layer_trust(n) for n in range(1, pages + 1)], pages
        )

        total = sum(timings.values())
        print(f"  {'sum':24} {total * 1000:8.1f} ms/page")
        print(f"  projected for all {units} pages: {total * units / 60:.1f} min")

        # The narrowed enumeration, which is what the probe and each window use.
        started = time.monotonic()
        seeds = list(session.enumerate_units(pages=range(1, pages + 1)))
        narrow = time.monotonic() - started
        print(f"  enumerate {len(seeds)} pages (narrowed): {narrow:.2f}s  ({narrow / pages * 1000:.0f} ms/page)")
        sample = max(1, units // 60)
        wanted = [page for page in range(1, units + 1) if page % sample == 0 or page in (1, units)]
        print(f"  probe would read {len(wanted)} of {units} pages -> ~{len(wanted) * narrow / pages:.1f}s instead of ~{units * narrow / pages:.1f}s")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    arguments = [int(a) for a in sys.argv[2:]]
    raise SystemExit(main(sys.argv[1], *arguments))
