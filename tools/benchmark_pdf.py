#!/usr/bin/env python3
"""Per-operator benchmark for PDF extraction.

Performance work needs numbers before and after, on the same documents, or it is
just opinion. This times each operator on representative pages so a regression is
visible and an improvement is provable.

    tools/benchmark_pdf.py dataset/Chucky-*.pdf dataset/[sigmod2022-*.pdf

Reports per-operator seconds, seconds/page, and the share of the total, plus the
end-to-end page rate. Run it before and after a change and compare.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docpipe.api.types import Cost, DocumentRecord, OpContext, PageState  # noqa: E402
from docpipe.builtins import build_registry  # noqa: E402
from docpipe.config import Config  # noqa: E402
from docpipe.ingest.base import doc_id_for  # noqa: E402
from docpipe.schema import PageRecord  # noqa: E402

#: The operators a born-digital PDF actually runs, in pipeline order.
PIPELINE = (
    ("pdf_text_layer", {}),
    ("pdf_font_heading_map", {}),
    ("pdf_tables", {"strategy": "lines"}),
    ("caption_blocks", {}),
    ("reading_order_v1", {}),
)

#: Pages sampled per document. Enough to be representative, few enough to iterate.
DEFAULT_SAMPLE = 8


def time_document(path: Path, workers_sample: int, registry, config: Config) -> dict[str, object]:
    from docpipe.ingest.pdf import PdfSession

    doc_id = doc_id_for(path)
    session = PdfSession(path=path, doc_id=doc_id, config=config, work_dir=Path(config.work_dir))
    total_pages = session.units()
    step = max(1, total_pages // workers_sample)
    page_numbers = sorted({*range(1, total_pages + 1, step), total_pages})[: workers_sample + 1]

    timings: dict[str, list[float]] = {name: [] for name, _ in PIPELINE}
    opened = 0
    try:
        for page_no in page_numbers:
            record = PageRecord(doc_id=doc_id, page_no=page_no, page_id=f"{doc_id}:p{page_no}", kind="pdf_page")
            state = PageState(record=record)
            context = OpContext(
                doc=DocumentRecord(
                    doc_id=doc_id, source_path=str(path), format="pdf", bytes=path.stat().st_size, sha256=doc_id
                ),
                state=state,
                work_dir=Path(config.work_dir),
                out_dir=Path(config.out_dir),
                config=config,
                rasterer=session,
                session=session,
                log=lambda *a, **k: None,
            )
            text = session.page_text(page_no)
            record.text_layer = text
            record.text_layer_chars = len(text.strip())

            for name, params in PIPELINE:
                spec = registry.ops[name]
                started = time.perf_counter()
                try:
                    result = spec.fn(context, params)
                    if result.ok:
                        state.merge(result, name)
                except Exception as exc:  # noqa: BLE001 - a benchmark records, it does not fail
                    print(f"  {name} raised {type(exc).__name__}: {exc}", file=sys.stderr)
                timings[name].append(time.perf_counter() - started)
            # page_text is measured separately: it is the floor every other op sits on
            started = time.perf_counter()
            session.page_text(page_no)
            timings.setdefault("(text layer)", []).append(time.perf_counter() - started)
    finally:
        session.close()

    return {
        "path": path.name,
        "pages": total_pages,
        "sampled": len(page_numbers),
        "timings": timings,
        "opened": opened,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-operator PDF benchmark.")
    parser.add_argument("documents", nargs="+", type=Path)
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="pages sampled per document")
    parser.add_argument("--out", default="/tmp/docpipe-bench", help="scratch directory")
    args = parser.parse_args(argv)

    config = Config(out_dir=Path(args.out) / "out", work_dir=Path(args.out) / "work", offline=True, workers=1)
    config.ensure_dirs()
    registry = build_registry(load_entrypoints=False)

    grand_total: dict[str, float] = {}
    pages_measured = 0
    for path in args.documents:
        if not path.is_file():
            print(f"skip (missing): {path}", file=sys.stderr)
            continue
        result = time_document(path, args.sample, registry, config)
        timings = result["timings"]  # type: ignore[assignment]
        sampled = int(result["sampled"])
        pages_measured += sampled
        print(f"\n=== {result['path']} ({result['pages']} pages, {sampled} sampled) ===")
        total = sum(sum(v) for v in timings.values())
        print(f"{'operator':<22}{'sec/page':>10}{'total':>9}{'share':>8}")
        for name in list(timings):
            values = timings[name]
            if not values:
                continue
            per_page = statistics.median(values)
            print(f"{name:<22}{per_page:>10.3f}{sum(values):>9.2f}{100 * sum(values) / total:>7.1f}%")
            grand_total[name] = grand_total.get(name, 0.0) + sum(values)
        print(f"{'TOTAL':<22}{total / sampled:>10.3f}{total:>9.2f}")
        print(f"page rate: {sampled / total:.2f} pages/s (single worker)")

    if len(args.documents) > 1:
        print("\n=== aggregate ===")
        total = sum(grand_total.values())
        for name, seconds in sorted(grand_total.items(), key=lambda kv: -kv[1]):
            print(f"{name:<22}{seconds:>9.2f}{100 * seconds / total:>7.1f}%")
        print(f"{'TOTAL':<22}{total:>9.2f} over {pages_measured} pages")
        print(f"page rate: {pages_measured / total:.2f} pages/s (single worker)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
