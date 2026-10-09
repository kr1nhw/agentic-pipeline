#!/usr/bin/env python3
"""Two candidate signals for "this text layer is OCR", measured side by side.

The font signal alone is not enough, and the corpus proves it from both sides:

  Congressional Record   329 fonts, 284 WinAnsi, no unicode maps  -> text is PERFECT
  ACL 2021 naacl-main.1   26 fonts, all Builtin/Custom, no maps   -> text is PERFECT
  1950s topology paper   system font, no map                      -> text is GARBLED

Metadata cannot separate the first two from the third, so the second opinion has
to come from the text itself. The candidate is word shape: in garbled output the
whitespace-separated tokens are mixtures of letters and symbols (`~'(R)`, `R tl]`,
`U{Ry`), while real prose is made of letter-only tokens.

This prints both, per document, so the threshold is chosen from measurements of
documents whose right answer is known by eye.

    tools/rpy.sh --docker tools/probe_trust.py <dir> [dir...]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")

STANDARD_ENCODINGS = {"winansi", "macroman", "standard", "pdfdoc", "macexpert", "symbol", "winansibold"}
WORD_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def font_stats(path: Path) -> dict[str, float]:
    proc = subprocess.run(["pdffonts", str(path)], capture_output=True, text=True, check=False)
    lines = proc.stdout.splitlines()
    if len(lines) < 3:
        return {"total": 0, "unicode_ratio": 1.0, "standard_encoding_ratio": 1.0}
    header = lines[0]
    labels = ["name", "type", "encoding", "emb", "sub", "uni", "object", "ID"]
    starts = [(label, header.find(label)) for label in labels]
    starts = [(label, at) for label, at in starts if at >= 0]
    total = unicode_ok = standard = 0
    for line in lines[2:]:
        if not line.strip():
            continue
        row = {}
        for order, (label, at) in enumerate(starts):
            end = starts[order + 1][1] if order + 1 < len(starts) else len(line)
            row[label] = line[at:end].strip() if at < len(line) else ""
        if not row.get("name"):
            continue
        total += 1
        if row.get("uni") == "yes":
            unicode_ok += 1
        if row.get("encoding", "").lower() in STANDARD_ENCODINGS:
            standard += 1
    if not total:
        return {"total": 0, "unicode_ratio": 1.0, "standard_encoding_ratio": 1.0}
    return {
        "total": total,
        "unicode_ratio": unicode_ok / total,
        "standard_encoding_ratio": standard / total,
    }


def word_shape(text: str) -> tuple[float, int]:
    """Fraction of tokens that are made of letters only (plus - and ')."""
    tokens = [token for token in text.split() if any(ch.isalnum() for ch in token)]
    if len(tokens) < 20:
        return 1.0, len(tokens)
    good = 0
    for token in tokens:
        core = token.strip(".,;:!?()[]{}<>\"'`/*_+=|\\")
        if len(core) >= 2 and all(ch in WORD_CHARS or ch in "-'" for ch in core):
            good += 1
    return good / len(tokens), len(tokens)


def main(directories: list[str]) -> int:
    from docpipe.config import Config
    from docpipe.ingest import pdf as pdfmod

    paths: list[Path] = []
    for directory in directories:
        base = Path(directory)
        if base.is_dir():
            paths.extend(sorted(base.glob("*.pdf")))
        elif base.is_file():
            paths.append(base)

    print(f"{'document':44} {'fonts':>5} {'uni%':>5} {'std%':>5} {'words':>6} {'shape':>6} {'trust':>6}")
    for path in paths:
        stats = font_stats(path)
        try:
            session = pdfmod.open_session(path, "probe", Config(offline=True))
        except Exception as exc:  # noqa: BLE001
            print(f"{path.name[:44]:44} unreadable: {exc}")
            continue
        try:
            shapes = []
            for page in range(1, min(3, session.units()) + 1):
                ratio, _ = word_shape(session.page_text(page))
                shapes.append(ratio)
            shape = sum(shapes) / len(shapes) if shapes else 1.0
            trust = session.text_layer_trust(1)
        finally:
            session.close()
        print(
            f"{path.name[:44]:44} {stats['total']:5} {stats['unicode_ratio']*100:5.0f} "
            f"{stats['standard_encoding_ratio']*100:5.0f} {len(shapes):6} {shape:6.2f} {trust:6.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
