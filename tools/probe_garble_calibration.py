#!/usr/bin/env python3
"""Calibrate the garbling measure on pages whose right answer is known.

The current rule is alphanumeric characters over ALL characters, which counts
newlines and the indentation a layout-preserving extractor emits. A chart page is
then mostly whitespace by character count and reads as garbled:

    '80\\n              100\\n              75\\n'   ->  0.27

Every character there is meaningful. The corpus also contains pages whose text
layer is deliberately corrupted (the synthetic `ocr-layer` documents, built to
reproduce the `emb=no uni=no` signature of an OCR'd scan), so both sides of the
question are present and the threshold can be chosen from measurements.

    tools/rpy.sh --docker tools/probe_garble_calibration.py <run-dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/venv/lib/python3.12/site-packages")

CURRENT_LIMIT = 0.6  # docpipe.evaluate.GARBLE_LIMIT, reported for context


def measures(text: str) -> dict[str, float]:
    stripped = text.strip()
    if not stripped:
        return {"all": 1.0, "nonws": 1.0, "no_space": 1.0}
    alnum = sum(1 for char in stripped if char.isalnum())
    nonws = "".join(char for char in stripped if not char.isspace())
    return {
        "all": alnum / len(stripped),
        "nonws": (sum(1 for char in nonws if char.isalnum()) / len(nonws)) if nonws else 1.0,
        "no_space": len(stripped.split()) / max(1, len(stripped.split("\n"))),
    }


def main(run_dir_str: str) -> int:
    import pyarrow.parquet as pq

    run_dir = Path(run_dir_str)
    report = json.loads((run_dir / "report.json").read_text())
    evaluation = json.loads((run_dir / "evaluation.json").read_text())

    kind_of: dict[str, str] = {}
    for doc in report["docs"]:
        name = doc["source_path"].split("/")[-1]
        for page in evaluation["documents"]:
            if page["source_path"].split("/")[-1] == name:
                for unit in page.get("pages") or []:
                    kind_of[unit["page_id"]] = "corrupted-layer" if "ocr-layer" in doc["source_path"] else "ordinary"

    rows = pq.read_table(run_dir / "pages.parquet").to_pylist()
    groups: dict[str, list[dict[str, float]]] = {"corrupted-layer": [], "ordinary": []}
    for row in rows:
        text = row.get("text") or row.get("text_layer") or ""
        groups[kind_of.get(row["page_id"], "ordinary")].append(measures(text))

    print(f"{len(rows)} units: {len(groups['corrupted-layer'])} from known-corrupted layers, "
          f"{len(groups['ordinary'])} ordinary")
    for name, values in groups.items():
        if not values:
            continue
        for key in ("all", "nonws"):
            series = sorted(value[key] for value in values)
            low = series[len(series) // 20]
            median = series[len(series) // 2]
            print(f"  {name:16} {key:6}: p5={low:.3f} median={median:.3f} min={series[0]:.3f}")

    flagged_now = sum(1 for value in groups["ordinary"] if value["all"] < CURRENT_LIMIT)
    print(f"\n  ordinary pages below the current limit ({CURRENT_LIMIT}): {flagged_now}")
    for limit in (0.4, 0.5, 0.6, 0.7):
        ordinary = sum(1 for value in groups["ordinary"] if value["nonws"] < limit)
        corrupted = sum(1 for value in groups["corrupted-layer"] if value["nonws"] < limit)
        total_corrupted = len(groups["corrupted-layer"])
        print(
            f"  non-whitespace limit {limit:.1f}: flags {ordinary:4} ordinary page(s), "
            f"catches {corrupted}/{total_corrupted} corrupted page(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/data/mm-lbfs/out/corpus-r11"))
