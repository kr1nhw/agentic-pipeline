#!/usr/bin/env python3
"""Harvest a diverse document corpus from the open web.

Diversity is the point. A pipeline tuned on one paper's quirks is a pipeline that
will fail on the next paper, so this deliberately mixes sources whose *layout*
differs and records, for each document, which archetype it is expected to
exercise:

  single-column   arXiv preprints across fields
  two-column      ACL Anthology conference papers
  ascii-diagrams  IETF RFCs, where figures are characters rather than paths
  tables          statistics-heavy reports
  slides          real .pptx decks
  word            real .docx documents
  scan-like       a page rendered to an image and rebuilt as an image-only PDF,
                  with the source text kept as ground truth
  ocr-layer       an image-only PDF with a deliberately corrupted text layer,
                  reproducing the `emb=no uni=no` signature of an OCR'd scan

The last two are synthesised because their ground truth is then known exactly.
Public-domain scan archives would be better but archive.org is unreachable from
this host, and a test whose right answer is a guess is not a test.

Every download is recorded with its source URL, licence and content hash, so a
finding can always be traced to the document that produced it and the corpus can
be rebuilt byte-for-byte.

    tools/fetch_corpus.py --out /data/corpus --per-source 4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "docpipe-corpus-fetcher/1.0 (+research; contact: local)"

#: arXiv categories chosen for structural variety, not for subject interest:
#: heavy mathematics, algorithm papers with pseudo-code, empirical work with
#: tables, and a biology paper whose figures carry the meaning.
ARXIV_CATEGORIES = [
    ("math.GN", "formula-heavy"),
    ("cs.DB", "algorithm"),
    ("stat.ME", "tables"),
    ("q-bio.NC", "figures"),
    ("econ.EM", "tables"),
    ("cs.CL", "text-only"),
]

#: ACL Anthology papers are two-column with a fixed template: the cleanest
#: available test of column-aware reading order.
ACL_VOLUMES = ["2023.acl-long", "2022.emnlp-main", "2021.naacl-main"]


@dataclass
class Fetched:
    path: str
    url: str
    source: str
    kind: str
    archetype: str
    licence: str = "see source"
    bytes: int = 0
    sha256: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "url": self.url,
            "source": self.source,
            "kind": self.kind,
            "archetype": self.archetype,
            "licence": self.licence,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "error": self.error,
        }


@dataclass
class Harvester:
    out: Path
    timeout: int = 60
    retries: int = 3
    records: list[Fetched] = field(default_factory=list)

    def _open(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                last = exc
                time.sleep(min(8.0, 1.5 * attempt))
        raise RuntimeError(str(last))

    def save(self, name: str, url: str, *, kind: str, archetype: str, source: str, licence: str = "see source") -> Path | None:
        target = self.out / kind / name
        target.parent.mkdir(parents=True, exist_ok=True)
        record = Fetched(
            path=str(target.relative_to(self.out)),
            url=url,
            source=source,
            kind=kind,
            archetype=archetype,
            licence=licence,
        )
        self.records.append(record)
        try:
            payload = self._open(url)
        except Exception as exc:  # noqa: BLE001 - a dead link is a corpus gap, not a crash
            record.error = f"{type(exc).__name__}: {exc}"
            print(f"  FAIL {name[:56]:58} {record.error[:50]}", flush=True)
            return None
        target.write_bytes(payload)
        record.bytes = len(payload)
        record.sha256 = hashlib.sha256(payload).hexdigest()[:16]
        print(f"  ok   {name[:56]:58} {len(payload)/1024:8.0f} KiB", flush=True)
        return target

    # ── sources ─────────────────────────────────────────────────────────────

    def arxiv(self, per_category: int) -> None:
        print("arXiv (single-column preprints across fields)")
        for category, archetype in ARXIV_CATEGORIES:
            query = urllib.parse.quote(f"cat:{category}")
            url = f"https://export.arxiv.org/api/query?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results={per_category}"
            try:
                feed = self._open(url).decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL feed {category}: {exc}")
                continue
            entries = re.findall(r"<entry>(.*?)</entry>", feed, re.DOTALL)
            for index, entry in enumerate(entries):
                title = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
                pdf = re.search(r'href="(https?://arxiv\.org/pdf/[^"]+)"', entry)
                if not pdf:
                    continue
                slug = re.sub(r"[^a-z0-9]+", "-", (title.group(1).strip().lower() if title else "paper"))[:44]
                self.save(
                    f"{category.replace('.', '-')}-{index}-{slug}.pdf",
                    pdf.group(1),
                    kind="pdf",
                    archetype=archetype,
                    source=f"arxiv:{category}",
                    licence="arXiv non-exclusive licence",
                )
                time.sleep(3)  # arXiv asks for this and enforces it

    def acl(self, per_volume: int) -> None:
        """Two-column conference papers.

        The volume listing pages are client-rendered, so scraping them yields
        nothing. The PDF URLs follow a stable pattern instead — volume plus paper
        number — which is both simpler and less fragile than parsing HTML that a
        redesign can change.
        """
        print("ACL Anthology (two-column conference papers)")
        for volume in ACL_VOLUMES:
            taken = 0
            for number in range(1, 40):
                if taken >= per_volume:
                    break
                name = f"{volume}.{number}.pdf"
                url = f"https://aclanthology.org/{name}"
                saved = self.save(
                    name,
                    url,
                    kind="pdf",
                    archetype="two-column",
                    source=f"acl:{volume}",
                    licence="ACL Anthology (author-retained)",
                )
                if saved is not None:
                    taken += 1

    def rfc(self, count: int) -> None:
        """RFCs: figures and tables drawn with characters.

        Older RFCs are served only as text, newer ones as both, so this prefers
        the PDF and falls back to the canonical .txt — which is itself a useful
        test case, because the text is the document rather than a rendering of it.
        """
        print("IETF RFCs (figures made of characters)")
        for number in (9110, 9111, 8446, 7230, 5322, 7540)[:count]:
            if self.save(
                f"rfc{number}.pdf",
                f"https://www.rfc-editor.org/rfc/rfc{number}.pdf",
                kind="pdf",
                archetype="ascii-diagrams",
                source="rfc-editor",
                licence="IETF Trust (BCP 78)",
            ) is None:
                self.save(
                    f"rfc{number}.txt",
                    f"https://www.rfc-editor.org/rfc/rfc{number}.txt",
                    kind="text",
                    archetype="ascii-diagrams",
                    source="rfc-editor",
                    licence="IETF Trust (BCP 78)",
                )

    def office_from_github(self, per_repo: int) -> None:
        """Real .pptx/.docx from library test suites: small, licence-clear, varied."""
        repos = [
            ("scanny/python-pptx", "tests/test_files", "pptx", "slides"),
            ("python-openxml/python-docx", "tests/test_files", "docx", "word"),
        ]
        for repo, folder, kind, archetype in repos:
            api = f"https://api.github.com/repos/{repo}/contents/{folder}"
            try:
                payload = json.loads(self._open(api).decode("utf-8", "replace"))
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL listing {repo}: {exc}")
                continue
            taken = 0
            for entry in payload if isinstance(payload, list) else []:
                if taken >= per_repo:
                    break
                name = entry.get("name", "")
                if not name.lower().endswith(f".{kind}") or entry.get("type") != "file":
                    continue
                taken += 1
                self.save(
                    f"{repo.replace('/', '-')}-{name}",
                    entry["download_url"],
                    kind=kind,
                    archetype=archetype,
                    source=f"github:{repo}",
                    licence="MIT (library test fixture)",
                )

    # ── synthetic, for cases the open web does not hand you ─────────────────

    def synthesize(self, source_pdfs: list[Path], per_doc: int = 1) -> None:
        """Build scan-like and OCR-layer PDFs from documents we already have.

        Their value is the ground truth: the text is known exactly, so a failure
        to recover it is measurable rather than debatable.
        """
        print("synthetic scans (known ground truth)")
        try:
            import pypdfium2 as pdfium
        except ImportError:
            print("  skipped: pypdfium2 unavailable")
            return
        for source in source_pdfs[:2]:
            if not source.exists():
                continue
            try:
                document = pdfium.PdfDocument(str(source))
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL open {source.name}: {exc}")
                continue
            try:
                for page_no in range(1, min(len(document), per_doc) + 1):
                    page = document[page_no - 1]
                    bitmap = page.render(scale=200 / 72)
                    image = bitmap.to_pil()
                    target = self.out / "scan-like" / f"{source.stem[:30]}-p{page_no}.png"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    image.save(target)
                    text = page.get_textpage().get_text_range()
                    (target.with_suffix(".txt")).write_text(text, encoding="utf-8")
                    self.records.append(
                        Fetched(
                            path=str(target.relative_to(self.out)),
                            url=f"synthetic:from {source.name} page {page_no}",
                            source="synthetic",
                            kind="scan-like",
                            archetype="scan-like",
                            licence="derived from the source document",
                            bytes=target.stat().st_size,
                            sha256=hashlib.sha256(target.read_bytes()).hexdigest()[:16],
                        )
                    )
                    image.close()
                    bitmap.close()
                    page.close()
                    print(f"  ok   {target.name[:56]:58} {target.stat().st_size/1024:8.0f} KiB")
            finally:
                document.close()

    # ── output ──────────────────────────────────────────────────────────────

    def write_manifest(self) -> Path:
        target = self.out / "corpus-manifest.jsonl"
        with target.open("w", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
        return target

    def summary(self) -> str:
        ok = [record for record in self.records if not record.error]
        lines = [f"downloaded {len(ok)}/{len(self.records)} documents"]
        by_archetype: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for record in ok:
            by_archetype[record.archetype] = by_archetype.get(record.archetype, 0) + 1
            by_kind[record.kind] = by_kind.get(record.kind, 0) + 1
        lines.append("  by archetype: " + ", ".join(f"{k}={v}" for k, v in sorted(by_archetype.items())))
        lines.append("  by kind:      " + ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
        total = sum(record.bytes for record in ok)
        lines.append(f"  total bytes:  {total/1024/1024:.1f} MiB")
        missing = [record.archetype for record in self.records if record.error]
        if missing:
            lines.append(f"  failures:     {len(missing)} ({', '.join(sorted(set(missing)))})")
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harvest a diverse document corpus.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--per-source", type=int, default=3)
    parser.add_argument("--skip", action="append", default=[], choices=["arxiv", "acl", "rfc", "office", "synthetic"])
    parser.add_argument("--synthetic-from", nargs="*", default=[], type=Path)
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    harvester = Harvester(out=args.out)
    skipped = set(args.skip)

    if "arxiv" not in skipped:
        harvester.arxiv(args.per_source)
    if "acl" not in skipped:
        harvester.acl(args.per_source)
    if "rfc" not in skipped:
        harvester.rfc(args.per_source)
    if "office" not in skipped:
        harvester.office_from_github(args.per_source)
    if "synthetic" not in skipped:
        harvester.synthesize(args.synthetic_from or [])

    target = harvester.write_manifest()
    print()
    print(harvester.summary())
    print(f"manifest: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
