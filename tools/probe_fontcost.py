#!/usr/bin/env python3
"""Count how many subprocesses one page of trust checking actually costs.

The batch run spends minutes on the Federal Register with `pdffonts` processes
appearing throughout, and its CPU use stays near idle, which is what a per-page
subprocess on a 6 MB file would look like. `font_report` is supposed to be cached
per session; this counts the calls instead of trusting the cache.

    tools/rpy.sh --docker tools/probe_fontcost.py /data/mm-lbfs/corpus-new/pdf/FR-2024-01-10.pdf
"""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")

SHIM_DIR = Path("/tmp/shim")
COUNTER = Path("/tmp/shim/count")


def install_shim(name: str) -> None:
    """Put a counting wrapper ahead of the real binary on PATH."""
    SHIM_DIR.mkdir(exist_ok=True)
    real = None
    for directory in os.environ["PATH"].split(":"):
        candidate = Path(directory) / name
        if candidate.is_file():
            real = candidate
            break
    if real is None:
        raise SystemExit(f"{name} not on PATH")
    script = SHIM_DIR / name
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" {name} >> {COUNTER}\n'
        f'exec {real} "$@"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{SHIM_DIR}:{os.environ['PATH']}"
    print(f"shim: {script} -> {real}")


def counts() -> dict[str, int]:
    if not COUNTER.exists():
        return {}
    tally: dict[str, int] = {}
    for line in COUNTER.read_text().split():
        tally[line] = tally.get(line, 0) + 1
    return tally


def main(path_str: str, pages: int = 24) -> int:
    install_shim("pdffonts")
    install_shim("pdftotext")

    from docpipe.config import Config
    from docpipe.ingest import pdf as pdfmod

    path = Path(path_str)
    session = pdfmod.open_session(path, "probe", Config(offline=True))
    try:
        units = session.units()
        print(f"{path.name}: {units} units")
        started = time.monotonic()
        trust = [session.text_layer_trust(page) for page in range(1, min(pages, units) + 1)]
        elapsed = time.monotonic() - started
        print(f"trust for {len(trust)} pages: {elapsed:.1f}s  values {sorted(set(trust))}")
        print("subprocess calls:", counts())

        # Now the enumerate path, which is what the probe and each window run.
        COUNTER.unlink(missing_ok=True)
        started = time.monotonic()
        seeds = list(session.enumerate_units())
        elapsed = time.monotonic() - started
        print(f"enumerate {len(seeds)} units: {elapsed:.1f}s  subprocess calls: {counts()}")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[2:]]
    raise SystemExit(main(sys.argv[1], *args))
