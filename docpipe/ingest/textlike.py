"""Markdown, plain text and source code ingest.

These formats need no renderer, no model and almost no logic — which is exactly
why they are the cheapest paths in the catalog and why the router should reach
them first. The only real decision is where the unit boundaries are: Markdown has
explicit structure, prose does not, and code has both.

Units stream, and a file that is one unit is still read in chunks rather than
whole, so a 200 MB log file behaves like any other document.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from docpipe.api.types import FormatContext, PageSignals, UnitSeed
from docpipe.ingest.base import BaseSession

TEXT_EXTENSIONS = (
    ".md", ".markdown", ".mdx", ".txt", ".text", ".rst", ".log",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv",
)
CODE_EXTENSIONS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".scala",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".ps1", ".sql", ".lua", ".pl", ".r", ".jl", ".dart",
    ".ex", ".exs", ".clj", ".hs", ".ml", ".vue", ".svelte", ".css", ".scss", ".html",
)

#: Chunk size for prose with no internal structure.
CHUNK_CHARS = 6000

#: Read at most this much for classification. A document is typed from its head.
SAMPLE_CHARS = 4000

LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".scala": "scala", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift", ".sh": "bash",
    ".bash": "bash", ".zsh": "bash", ".ps1": "powershell", ".sql": "sql", ".lua": "lua",
    ".pl": "perl", ".r": "r", ".jl": "julia", ".dart": "dart", ".ex": "elixir",
    ".clj": "clojure", ".hs": "haskell", ".ml": "ocaml", ".vue": "vue",
    ".svelte": "svelte", ".css": "css", ".scss": "scss", ".html": "html",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".json": "json",
    ".md": "markdown", ".rst": "rst",
}

ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
SETEXT_UNDERLINE = re.compile(r"^(=+|-{2,})\s*$")


def guess_language(path: Path) -> str:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


def is_code(path: Path) -> bool:
    return path.suffix.lower() in CODE_EXTENSIONS


def read_text(path: Path, encoding_hint: str | None = None) -> str:
    """Decode a text file without ever guessing wrong silently.

    The order is deliberate: the caller's hint, then UTF-8, then the two encodings
    that actually show up in this corpus, then a lossy decode that reports itself.
    """
    raw = path.read_bytes()
    for encoding in filter(None, (encoding_hint, "utf-8", "utf-8-sig", "gb18030", "latin-1")):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class TextSession(BaseSession):
    def __init__(self, path: Path, doc_id: str, config: Any, work_dir: Path) -> None:
        super().__init__(path=path, doc_id=doc_id, format_id="text", config=config, work_dir=work_dir)
        self.text = read_text(path)
        self.language = guess_language(path)
        self.code = is_code(path)
        self._sections = self._split_markdown() if self.language == "markdown" else None
        self._units = len(self._sections) if self._sections else max(1, -(-len(self.text) // CHUNK_CHARS))

    def close(self) -> None:
        return None

    # ── structure ───────────────────────────────────────────────────────────

    def _split_markdown(self) -> list[dict[str, Any]]:
        """Split on ATX/setext headings, tracking the heading path.

        The heading path rides along on every unit: it is what makes a chunk
        retrievable later, and recomputing it downstream is how provenance gets
        lost.
        """
        lines = self.text.splitlines()
        sections: list[dict[str, Any]] = []
        stack: list[tuple[int, str]] = []
        current: dict[str, Any] = {"path": [], "heading": None, "lines": []}

        def flush() -> None:
            body = "\n".join(current["lines"]).strip()
            if body or current["heading"]:
                sections.append(
                    {
                        "path": list(current["path"]),
                        "heading": current["heading"],
                        "level": current.get("level") or 1,
                        "text": body,
                    }
                )

        index = 0
        while index < len(lines):
            line = lines[index]
            match = ATX_HEADING.match(line)
            level: int | None = None
            title = ""
            consumed = 1
            if match:
                level = len(match.group(1))
                title = match.group(2).strip()
            elif index + 1 < len(lines) and line.strip() and SETEXT_UNDERLINE.match(lines[index + 1]):
                level = 1 if lines[index + 1].strip().startswith("=") else 2
                title = line.strip()
                consumed = 2
            if level is not None:
                flush()
                stack = [entry for entry in stack if entry[0] < level]
                stack.append((level, title))
                current = {"path": [t for _, t in stack], "heading": title, "level": level, "lines": []}
                index += consumed
                continue
            current["lines"].append(line)
            index += 1
        flush()
        return sections or [{"path": [], "heading": None, "text": self.text}]

    def _chunk_plain(self) -> list[dict[str, Any]]:
        """Prose with no structure: fixed-size chunks on line boundaries."""
        chunks: list[dict[str, Any]] = []
        buffer: list[str] = []
        size = 0
        for line in self.text.splitlines():
            buffer.append(line)
            size += len(line) + 1
            if size >= CHUNK_CHARS:
                chunks.append({"path": [], "heading": None, "text": "\n".join(buffer)})
                buffer, size = [], 0
        if buffer:
            chunks.append({"path": [], "heading": None, "text": "\n".join(buffer)})
        # A chunk that carries no content is not a unit. The tail of a split file
        # is routinely a stray newline, and calling that a page produced a unit
        # with one character that no operator could extract anything from — so
        # the gate flagged the document for review and the review found nothing
        # wrong with the document. A file with no content at all still yields one
        # empty unit, because "this file is empty" is a fact worth reporting.
        chunks = [chunk for chunk in chunks if chunk["text"].strip()]
        return chunks or [{"path": [], "heading": None, "text": ""}]

    def units_data(self) -> list[dict[str, Any]]:
        if self._sections is not None:
            return self._sections
        if self.code:
            # One unit per file: symbol boundaries are the code operator's job,
            # and splitting source mid-function destroys the structure we want.
            return [{"path": [], "heading": self.path.name, "text": self.text}]
        return self._chunk_plain()

    def enumerate_units(self) -> Iterator[UnitSeed]:
        for index, unit in enumerate(self.units_data(), start=1):
            text = self._unit_text(unit)
            signals = PageSignals(
                text_layer_chars=len(text.strip()),
                layout_hint="code" if self.code else ("markdown" if self.language == "markdown" else "prose"),
                text_sample=text.strip()[:220] or None,
            )
            yield UnitSeed(
                page_no=index,
                kind="code_unit" if self.code else "text_chunk",
                signals=signals,
                text_layer=text,
                payload={"heading_path": unit.get("path", []), "heading": unit.get("heading"), "language": self.language},
            )

    @staticmethod
    def _unit_text(unit: dict[str, Any]) -> str:
        """A unit's text, heading line included.

        Splitting on headings moves the heading out of the body, and the unit is
        only the body. Reconstructing the line here keeps `text_layer` what it
        claims to be — the file's own text for this unit — so the passthrough
        operator emits the heading instead of silently dropping the one line that
        gives the section its name.
        """
        heading = unit.get("heading")
        if not heading:
            return unit["text"]
        level = int(unit.get("level") or 1)
        prefix = "#" * max(1, min(6, level))
        body = unit["text"]
        return f"{prefix} {heading}\n\n{body}" if body else f"{prefix} {heading}"

    def deep_signals(self, page_no: int) -> dict[str, Any]:
        unit = self.units_data()[page_no - 1]
        text = unit["text"]
        data: dict[str, Any] = {
            "language": self.language,
            "lines": text.count("\n") + 1,
            "heading_path": unit.get("path", []),
        }
        if self.code:
            data["n_code_fences"] = 0
            data["defs"] = len(re.findall(r"^\s*(?:def|class|func|fn|function)\s+\w+", text, re.M))
        else:
            data["n_code_fences"] = text.count("```") // 2
            data["n_list_items"] = len(re.findall(r"^\s*(?:[-*+]|\d+\.)\s+", text, re.M))
        return data


# ─────────────────────────────────────────────────────────────────────────────
# Format plugin surface
# ─────────────────────────────────────────────────────────────────────────────


def match_text(head: bytes, path: str) -> float:
    lowered = path.lower()
    if lowered.endswith(TEXT_EXTENSIONS):
        return 1.0 if lowered.endswith((".md", ".markdown", ".txt", ".rst")) else 0.8
    # Fall back to a content sniff: mostly-printable bytes means text.
    if not head:
        return 0.0
    sample = head[:1024]
    if b"\x00" in sample:
        return 0.0
    printable = sum(1 for byte in sample if 9 <= byte <= 13 or 32 <= byte < 127 or byte >= 128)
    return 0.5 if printable / len(sample) > 0.95 else 0.0


def match_code(head: bytes, path: str) -> float:
    return 1.0 if path.lower().endswith(CODE_EXTENSIONS) else 0.0


def open_text(path: Path, doc_id: str, config: Any) -> TextSession:
    return TextSession(path=path, doc_id=doc_id, config=config, work_dir=Path(config.work_dir))


def enumerate_text(ctx: FormatContext) -> Iterator[UnitSeed]:
    session = TextSession(ctx.path, ctx.doc_id, ctx.scratch.get("config"), ctx.work_dir)
    yield from session.enumerate_units()
