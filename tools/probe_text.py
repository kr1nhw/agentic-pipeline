#!/usr/bin/env python3
"""Is a distrusted text layer actually wrong?

`text_layer_trust` refuses a page when most fonts are unembedded without unicode
maps, on the theory that the characters were inferred from glyph names rather
than read from a CMap. The theory holds for an OCR'd scan. It is a *prediction*
about the text, and the prediction is checkable: print the text and look.

    tools/rpy.sh --docker tools/probe_text.py <pdf> [page] [chars]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")


def main(path_str: str, page_no: int = 1, chars: int = 700) -> int:
    path = Path(path_str)
    print(f"file {path.name}  {path.stat().st_size / 1024:.0f} KiB")

    fonts = subprocess.run(["pdffonts", str(path)], capture_output=True, text=True, check=False).stdout
    lines = fonts.splitlines()
    print("pdffonts header :", lines[0] if lines else "(none)")
    for line in lines[2:12]:
        print("   ", line)
    total = len([line for line in lines[2:] if line.strip()])
    unembedded = len([line for line in lines[2:] if line.strip() and " no " in f" {line} "])
    print(f"fonts {total}, unembedded-ish {unembedded}")

    from docpipe.config import Config
    from docpipe.ingest import pdf as pdfmod

    config = Config(offline=True)
    session = pdfmod.open_session(path, "probe", config)
    try:
        report = session.font_report()
        print("font_report     :", {k: v for k, v in report.items() if k != "fonts"})
        print("trust           :", session.text_layer_trust(page_no))
        text = session.page_text(page_no)
        stripped = text.strip()
        print(f"text            : {len(stripped)} chars")
        # A garbled layer fails these in a way the eye confirms instantly.
        words = stripped.split()
        long_words = [w for w in words if len(w) > 18]
        odd = [w for w in words if sum(c.isalpha() for c in w) < len(w) * 0.4 and len(w) > 3]
        print(f"  words {len(words)}, >18 chars {len(long_words)}, mostly-non-alpha {len(odd)}")
        print("  sample:", " ".join(words[:60])[:400])
        print()
        print("--- text ---")
        print(stripped[:chars])
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    args = [int(a) for a in sys.argv[2:]]
    raise SystemExit(main(sys.argv[1], *args))
