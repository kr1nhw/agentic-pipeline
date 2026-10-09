#!/usr/bin/env python3
"""A slide with no text: is it empty, or is its content in a picture?

"no text layer and no blocks" is correct when a slide holds nothing and wrong when
it holds a full-page image, because those want opposite responses — leave it alone
versus read the picture. The signals decide, so this prints what the reader knows
about each flagged slide next to what the file actually contains.

    tools/rpy.sh --docker tools/probe_slide.py /data/mm-lbfs/corpus-office/pptx/KEY02.pptx
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
    session = office.open_pptx(path, "probe", config)
    print(f"{path.name}: {session.units()} slides")
    for page in range(1, session.units() + 1):
        shapes = session.slide_shapes(page)
        text = "\n".join(shape["text"] for shape in shapes if shape.get("text"))
        kinds: dict[str, int] = {}
        for shape in shapes:
            kinds[shape.get("kind") or shape.get("type") or "?"] = kinds.get(shape.get("kind") or shape.get("type") or "?", 0) + 1
        pictures = kinds.get("picture", 0) + kinds.get("image", 0)
        signals = session.deep_signals(page)
        interesting = {
            key: value
            for key, value in signals.items()
            if key in ("n_images", "n_pictures", "image_area_ratio", "n_shapes", "n_text_shapes", "coverage", "ink_ratio")
        }
        print(
            f"  slide {page:3}: shapes={len(shapes):3} text={len(text):6} pictures={pictures:2} "
            f"kinds={json.dumps(kinds)} signals={json.dumps(interesting)}"
        )
        for shape in shapes:
            body = (shape.get("text") or "").replace("\n", " ")
            if body.strip():
                print(f"      shape {shape.get('name')!r:28} {len(body):5}  {body[:60]!r}")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
