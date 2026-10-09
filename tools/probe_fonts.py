#!/usr/bin/env python3
"""What does pdffonts say about every PDF, next to whether its text is readable?

`text_layer_trust` refuses a page when most fonts lack a unicode map, on the
theory that characters were inferred from glyph names. GPO documents break the
theory: the Congressional Record is composed with 329 unembedded fonts, every one
of them without a unicode map, and its text extracts perfectly, because a font
with a *standard* encoding (WinAnsi) has a byte-to-character table that needs no
CMap at all.

This prints, per document, the encoding mix and the unicode mix, so the rule can
be written against evidence from both sides: documents that must be refused and
documents that must not be.

    tools/rpy.sh --docker tools/probe_fonts.py /data/mm-lbfs/corpus/pdf
"""

from __future__ import annotations

import collections
import subprocess
import sys
from pathlib import Path

#: Name-normalised standard encodings: a simple font with one of these has a
#: defined byte-to-character mapping, so extraction is a lookup, not a guess.
STANDARD_ENCODINGS = {"winansi", "macroman", "standard", "mac", "pdfdoc", "macExpert".lower(), "winansibold"}


def font_rows(path: Path) -> list[dict[str, str]]:
    """Parse pdffonts **by character column**, taken from the header.

    Splitting the line on whitespace does not work: the type column contains a
    space of its own (`Type 1C`, `Type 0`), so every field to its right shifts by
    one, and `emb` reads the encoding while `uni` reads the subset flag. The
    header line gives the start offset of each column, which is the only reliable
    map for a fixed-width table.
    """
    proc = subprocess.run(["pdffonts", str(path)], capture_output=True, text=True, check=False)
    lines = proc.stdout.splitlines()
    if len(lines) < 2:
        return []
    header = lines[0]
    labels = ["name", "type", "encoding", "emb", "sub", "uni", "object", "ID"]
    starts: list[tuple[str, int]] = []
    for label in labels:
        position = header.find(label)
        if position >= 0:
            starts.append((label, position))
    if not starts:
        return []
    rows = []
    for line in lines[2:]:
        if not line.strip():
            continue
        row: dict[str, str] = {}
        for order, (label, start) in enumerate(starts):
            end = starts[order + 1][1] if order + 1 < len(starts) else len(line)
            row[label] = line[start:end].strip() if start < len(line) else ""
        if row.get("name"):
            rows.append(row)
    return rows


def main(directory: str) -> int:
    paths = sorted(Path(directory).glob("*.pdf"))
    print(f"{len(paths)} PDFs in {directory}\n")
    print(f"{'document':46} {'fonts':>5} {'uni':>5} {'std-enc':>7}  encodings")
    for path in paths:
        rows = font_rows(path)
        if not rows:
            print(f"{path.name[:46]:46} {'-':>5}")
            continue
        unicode_ok = sum(1 for row in rows if row["uni"] == "yes")
        std = sum(1 for row in rows if row["encoding"].lower() in STANDARD_ENCODINGS)
        mix = collections.Counter(row["encoding"] for row in rows)
        top = ", ".join(f"{name}={count}" for name, count in mix.most_common(4))
        print(f"{path.name[:46]:46} {len(rows):5} {unicode_ok:5} {std:7}  {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/data/mm-lbfs/corpus/pdf"))
