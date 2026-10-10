#!/usr/bin/env python3
"""What separates a defective OCR layer from a good one?

The 1789 Statutes at Large carries an OCR layer that every existing measure passes:
its fonts have a Unicode map so the trust rule has no doubt to report, recall against
itself is 100 %, and word shape is 0.91-0.96 because inserted spaces and substituted
characters still produce word-like tokens. The text is visibly wrong.

Candidate signals, all language-neutral:
  single-letter rate   OCR spacing defects leave isolated letters (`Ses s`, `Ch .`)
  ocr agreement        a fresh local OCR of the same page, compared token by token
  image area           the precondition: only a page that IS an image has OCR text

    tools/rpy.sh --docker tools/probe_layer_defect.py <pdf> <page> [<pdf> <page> ...]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")

WORD = re.compile(r"[A-Za-z]+")


def tokens(text: str) -> list[str]:
    return [token.lower() for token in WORD.findall(text)]


def single_letter_rate(text: str) -> float:
    found = tokens(text)
    if not found:
        return 0.0
    return sum(1 for token in found if len(token) == 1) / len(found)


def agreement(layer: str, ocr: str) -> float:
    """Fraction of the layer's tokens that a fresh OCR also produced.

    A bag comparison, not a sequence alignment: the question is whether the two
    readers saw the same words, not whether they laid them out identically.
    """
    from collections import Counter

    left, right = Counter(tokens(layer)), Counter(tokens(ocr))
    if not left:
        return 0.0
    shared = sum(min(count, right.get(token, 0)) for token, count in left.items())
    return shared / sum(left.values())


def main(pairs: list[str]) -> int:
    from docpipe.config import Config
    from docpipe.ingest import pdf as pdfmod

    arguments = list(zip(pairs[0::2], pairs[1::2]))
    print(f"{'document':30} {'page':>5} {'chars':>7} {'img-area':>8} {'1-letter':>9} {'agreement':>9}")
    for path_str, page_str in arguments:
        path, page_no = Path(path_str), int(page_str)
        session = pdfmod.open_session(path, "probe", Config(offline=True, work_dir=Path("/tmp/ld")))
        try:
            layer = session.page_text(page_no)
            profile = session.page_profile(page_no)
            objects = session.page_objects(page_no)
            raster = session.render(page_no, 300)
            completed = subprocess.run(
                ["tesseract", str(raster), "stdout", "-l", "eng", "--psm", "3"],
                capture_output=True, timeout=240, check=False,
            )
            ocr = (completed.stdout or b"").decode("utf-8", "replace")
            print(
                f"{path.name[:30]:30} {page_no:5} {len(layer.strip()):7} "
                f"{profile.get('image_area_frac') or (1.0 if objects.get('images') else 0.0):8.2f} "
                f"{single_letter_rate(layer):9.3f} {agreement(layer, ocr):9.3f}"
            )
        finally:
            session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
