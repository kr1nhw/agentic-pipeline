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

#: A downloaded file is only kept if its first bytes agree with what it claims to
#: be. govinfo and friends answer a wrong-but-plausible package id with a 200 and
#: an HTML "not found" page, so status codes and extensions are both claims; the
#: magic number is the evidence. Without this the corpus silently gains a
#: document that is not a document, and every later measurement is wrong.
MAGIC: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF",),
    "pptx": (b"PK\x03\x04",),
    "docx": (b"PK\x03\x04",),
    "xlsx": (b"PK\x03\x04",),
    "zip": (b"PK\x03\x04",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "txt": (),  # any bytes are a valid text file; emptiness is checked instead
}


def magic_ok(kind: str, payload: bytes) -> tuple[bool, str]:
    """Does ``payload`` actually look like ``kind``? Returns (ok, reason)."""
    if kind == "txt":
        if not payload.strip():
            return False, "empty text file"
        return True, ""
    prefixes = MAGIC.get(kind)
    if prefixes is None:
        return True, ""
    if any(payload.startswith(prefix) for prefix in prefixes):
        return True, ""
    head = payload[:16]
    looks = "html" if head.lstrip()[:1] == b"<" else repr(head[:8])
    return False, f"not a {kind} ({looks}); server returned a placeholder page"

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

#: Government Publishing Office documents. `govinfo` serves every package as a
#: stable `/content/pkg/<id>/pdf/<id>.pdf`, and it is the only reachable source
#: of layouts that are hard for reasons other than "academic paper":
#:
#:   Congressional Record  three to four newspaper columns per page, dense
#:                         running text, speeches, and inserted tables. The
#:                         hardest column geometry in the corpus.
#:   US Code               two-column statute text with deep numbering, which
#:                         breaks any rule that assumes sections start at the
#:                         left margin.
#:   Federal Register      single-column rules with long tables and preambles.
#: Package ids that have been confirmed to return a real PDF. The obvious
#: guesses for bills, hearings and GAO reports (`BILLS-118hr1enr`,
#: `GAOREPORTS-GAO-24-106393`, `CHRG-118hhrg54321`) all answer **200 with a
#: 44 KiB HTML page**, which is why `magic_ok` exists: a 200 and a `.pdf`
#: extension agree with each other and neither is evidence.
GOVINFO = [
    ("CREC-2024-01-10", "three-column-newspaper"),
    ("CREC-2024-01-09", "three-column-newspaper"),
    ("USCODE-2022-title17", "two-column-legal"),
    ("FR-2024-01-10", "rules-and-long-tables"),
]

#: GovInfo's digitised historical volumes. These are SCANS: books printed in 1789 and
#: 1956, photographed and put through OCR, which is the one family the corpus
#: otherwise lacks and the reason the text-layer trust rule exists. They are large —
#: 150-300 MB each, because they are whole volumes — so each is fetched only when
#: asked for by name.
#:
#: The per-issue Congressional Record that govinfo serves is NOT a scan: the run
#: that produced this table checked CREC-1995-01-04 and CREC-1996-01-03 and found
#: text layers, 46 fonts and no images, i.e. born-digital from the start of the
#: collection. Guessing a pre-1994 issue id returns the usual 200-and-HTML
#: placeholder, which the magic-byte check rejects.
GOVINFO_SCANNED = [
    ("STATUTE-1", "scanned-historical-book"),
    ("STATUTE-70", "scanned-historical-book"),
]

#: IRS forms are the reachable sample of *form* documents: AcroForm fields,
#: checkboxes, boxed regions, and instructions that wrap around them. A pipeline
#: tuned on prose will happily read the boxes and lose the field semantics.
IRS_FORMS = [
    ("f1040.pdf", "form-fields"),
    ("f1120.pdf", "form-fields"),
    ("p15.pdf", "publication-two-column"),
]

#: Apache POI's test-data is a corpus of REAL user documents: files attached to bug
#: reports, contributed precisely because they broke a reader. That makes it the
#: best available source of .pptx/.docx that were not produced by a template or by
#: a library's own test suite, and the ones that are subtly wrong are the useful
#: ones. Files are sampled with a stride so the sample spans the list rather than
#: clustering on the first names, which are ordered by bug number.
POI_DIRECTORIES = [
    ("test-data/slideshow", ("pptx", "pptm"), "slides-real-world"),
    ("test-data/document", ("docx", "docm"), "word-real-world"),
]
#: Legacy binary Office, included deliberately as negative cases: a `.doc` or
#: `.ppt` is a document a real user has, it is NOT what python-docx/python-pptx
#: can open, and the pipeline must report that rather than crash or invent text.
POI_LEGACY = [
    ("test-data/document", "doc", "legacy-binary-word"),
    ("test-data/slideshow", "ppt", "legacy-binary-slides"),
]

#: SEC EDGAR filings. A 10-K's financial statements are the closest thing to a
#: real-world spreadsheet: multi-level headings, footnote markers, and tables
#: whose cells wrap. EDGAR also publishes `Financial_Report.xlsx` for many
#: filings, which is the only freely fetchable real spreadsheet found.
EDGAR_CIKS = ["0000320193", "0000789019", "0000019617"]
SEC_UA = "docpipe-corpus-fetcher/1.0 (research; contact: corpus@example.org)"


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
        ok, reason = magic_ok(kind, payload)
        if not ok:
            record.error = reason
            print(f"  BAD  {name[:56]:58} {reason[:46]}", flush=True)
            return None
        target.write_bytes(payload)
        record.bytes = len(payload)
        record.sha256 = hashlib.sha256(payload).hexdigest()[:16]
        print(f"  ok   {name[:56]:58} {len(payload)/1024:8.0f} KiB", flush=True)
        return target

    # ── sources ─────────────────────────────────────────────────────────────

    def poi_office(self, per_source: int) -> None:
        """Real-world OOXML documents from Apache POI's test-data."""
        print("Apache POI test-data (real-world pptx/docx from bug reports)")
        for directory, extensions, archetype in POI_DIRECTORIES:
            self._poi_directory(directory, extensions, archetype, per_source)
        for directory, extension, archetype in POI_LEGACY:
            self._poi_directory(directory, (extension,), archetype, 1)

    def _poi_directory(self, directory: str, extensions: tuple[str, ...], archetype: str, take: int) -> None:
        url = f"https://api.github.com/repos/apache/poi/contents/{directory}"
        try:
            listing = json.loads(self._open(url))
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL listing {directory}: {str(exc)[:60]}")
            return
        wanted = [
            item
            for item in listing
            if isinstance(item, dict)
            and item.get("name", "").rsplit(".", 1)[-1].lower() in extensions
            and item.get("download_url")
        ]
        if not wanted:
            print(f"  none  {directory} ({'/'.join(extensions)})")
            return
        # A stride, not a prefix: names are bug numbers, and the first ten are
        # whatever was reported first rather than whatever is representative.
        stride = max(1, len(wanted) // max(1, take))
        picked = wanted[::stride][:take]
        for item in picked:
            name = item["name"]
            self.save(
                name,
                item["download_url"],
                kind=extensions[0],
                archetype=archetype,
                source=f"poi:{directory}",
                licence="Apache-2.0 (apache/poi test-data)",
            )

    def govinfo(self, per_source: int) -> None:
        """GPO documents: three-column records, statutes, rules, reports."""
        print("govinfo (three-column records, legal text, rule tables)")
        for name, archetype in GOVINFO[: max(1, per_source)]:
            url = f"https://www.govinfo.gov/content/pkg/{name}/pdf/{name}.pdf"
            self.save(
                f"{name}.pdf",
                url,
                kind="pdf",
                archetype=archetype,
                source="govinfo",
                licence="public domain (US Government work)",
            )

    def govinfo_scanned(self, per_source: int) -> None:
        """Digitised historical volumes: real scans, large, fetched on request."""
        print("govinfo historical (scanned volumes, 150-300 MB each)")
        for name, archetype in GOVINFO_SCANNED[: max(1, per_source)]:
            url = f"https://www.govinfo.gov/content/pkg/{name}/pdf/{name}.pdf"
            # A 300 MB download needs more patience than the shared timeout gives.
            original, self.timeout = self.timeout, max(self.timeout, 900)
            try:
                self.save(
                    f"{name}.pdf",
                    url,
                    kind="pdf",
                    archetype=archetype,
                    source="govinfo-historical",
                    licence="public domain (US Government work)",
                )
            finally:
                self.timeout = original

    def irs_forms(self, per_source: int) -> None:
        """Government forms: AcroForm widgets, checkboxes, boxed fields."""
        print("IRS (forms with fields and boxed regions)")
        for name, archetype in IRS_FORMS[: max(1, per_source)]:
            self.save(name, f"https://www.irs.gov/pub/irs-pdf/{name}", kind="pdf", archetype=archetype,
                      source="irs", licence="public domain (US Government work)")

    def edgar(self, per_source: int) -> None:
        """Financial filings: tables whose cells wrap, and real spreadsheets.

        EDGAR rejects a UA without a contact address, which is why this needs its
        own header rather than the shared one.
        """
        print("SEC EDGAR (financial statements, tables, spreadsheets)")
        for cik in EDGAR_CIKS[: max(1, per_source)]:
            try:
                payload = self._open_sec(f"https://data.sec.gov/submissions/CIK{cik}.json")
                meta = json.loads(payload)
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL submissions {cik}: {str(exc)[:60]}")
                continue
            recent = meta.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accessions = recent.get("accessionNumber", [])
            for index, form in enumerate(forms):
                if form not in ("10-K", "10-Q") or index >= len(accessions):
                    continue
                acc = accessions[index].replace("-", "")
                cik_int = str(int(cik))
                base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}"
                # Ask the filing what it contains. `Financial_Report.xlsx` is a
                # plausible name that 404s on most filings; the index is the only
                # source of truth for the attachment names, and reading it also
                # keeps this working when EDGAR changes what it publishes.
                try:
                    listing = json.loads(self._open_sec(f"{base}/index.json"))
                    names = [item.get("name", "") for item in listing.get("directory", {}).get("item", [])]
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAIL index {cik}: {str(exc)[:60]}")
                    continue
                for name in names:
                    lowered = name.lower()
                    kind = ""
                    if lowered.endswith((".xlsx", ".xls")):
                        kind, archetype = "xlsx", "spreadsheet-financial"
                    elif lowered.endswith(".pdf"):
                        kind, archetype = "pdf", "filing-pdf"
                    if kind:
                        self.save(f"{cik}-{acc}-{name}", f"{base}/{name}", kind=kind,
                                  archetype=archetype, source=f"edgar:{cik}",
                                  licence="public filing (US SEC)")
                break

    def _open_sec(self, url: str) -> bytes:
        """EDGAR wants a contact address in the UA.

        No `Accept-Encoding: gzip` here on purpose: urllib does not decompress,
        so asking for gzip means handing the JSON parser gzip bytes.
        """
        request = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

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
        """Merge into the existing manifest rather than replacing it.

        Harvesting runs one source at a time — arxiv today, govinfo tomorrow —
        and a manifest that only describes the most recent run is worse than no
        manifest: it looks complete and is not.
        """
        target = self.out / "corpus-manifest.jsonl"
        merged: dict[str, dict[str, object]] = {}
        if target.is_file():
            for line in target.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    merged[str(existing.get("path"))] = existing
        for record in self.records:
            merged[record.path] = record.as_dict()
        with target.open("w", encoding="utf-8") as handle:
            for path in sorted(merged):
                handle.write(json.dumps(merged[path], ensure_ascii=False) + "\n")
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
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        choices=["arxiv", "acl", "rfc", "office", "synthetic", "govinfo", "irs", "edgar", "poi", "scanned"],
    )
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
    if "govinfo" not in skipped:
        harvester.govinfo(args.per_source)
    if "irs" not in skipped:
        harvester.irs_forms(args.per_source)
    if "edgar" not in skipped:
        harvester.edgar(args.per_source)
    if "poi" not in skipped:
        harvester.poi_office(args.per_source)
    if "scanned" not in skipped:
        harvester.govinfo_scanned(args.per_source)
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
