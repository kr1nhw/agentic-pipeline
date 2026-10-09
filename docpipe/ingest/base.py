"""Format detection and the ingest session contract.

A *session* is the per-worker object that owns one open document. It is created
once per document per worker and reused for every unit, because reopening a
500 MB PDF per page is how a scalable design stops being one.

Sessions are optional: a format may register only ``enumerate_units`` and be
extractable without rasterisation. Rasterisation is what unlocks every ``vl_*``
operator, so a format that cannot rasterise simply never reaches the expensive
path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Iterator, Protocol, runtime_checkable

from docpipe.api.types import CapabilityError, OpError, UnitSeed

#: Read this many bytes to identify a format. Enough for magic numbers and for a
#: container peek (ZIP/PDF headers), small enough to be free.
SNIFF_BYTES = 8192


def file_hash(path: Path, *, chunk: int = 1 << 20) -> str:
    """Streaming SHA-256: never reads a whole file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def doc_id_for(path: Path) -> str:
    """Stable identity for a document: content, not path, so moves are free."""
    return file_hash(path)[:16]


def sniff(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(SNIFF_BYTES)


@runtime_checkable
class DocumentSession(Protocol):
    """What the runner needs from an open document."""

    doc_id: str
    path: Path
    format_id: str

    def units(self) -> int: ...

    # `pages` restricts which units are produced. It is not an optimisation for
    # its own sake: producing a unit means reading the page — geometry, object
    # counts, layout, text — and on a 357-page issue that is the single largest
    # cost in the run. The probe needs about sixty pages and each execution
    # window needs its own range, so without this the whole document is profiled
    # three times over (once by the probe, once per window) to answer questions
    # about sixty and two hundred pages respectively.
    #
    # A session that ignores the argument stays correct and merely slow, which is
    # the right default for third-party formats.

    def enumerate_units(self, pages: Collection[int] | None = None) -> Iterator[UnitSeed]: ...

    def deep_signals(self, page_no: int) -> dict[str, Any]: ...

    def render(self, page_no: int, dpi: int) -> Path: ...

    def close(self) -> None: ...


@dataclass
class BaseSession:
    """Shared plumbing: unit counting, raster budget, lazy raster paths."""

    path: Path
    doc_id: str
    format_id: str
    config: Any
    work_dir: Path
    _units: int = 0

    def units(self) -> int:
        return self._units

    def enumerate_units(self, pages: Collection[int] | None = None) -> Iterator[UnitSeed]:  # pragma: no cover - overridden
        raise NotImplementedError

    def deep_signals(self, page_no: int) -> dict[str, Any]:
        return {}

    def render(self, page_no: int, dpi: int) -> Path:  # pragma: no cover - overridden
        raise OpError(f"{self.format_id} cannot rasterise pages")

    def raster(self, page_no: int, dpi: int, *, force: bool = False) -> Path:
        """The ``SupportsRaster`` entry point the runtime actually calls.

        Sessions implement ``render``; this adapts it. The distinction cost a
        silent failure once: every session defined ``render``, the protocol
        declared ``raster``, and *every* raster escalation — the only route to a
        vision call — failed with ``object has no attribute 'raster'`` while the
        page was quietly marked for review. A protocol nobody implements is worse
        than no protocol, because the type checker is not what runs.
        """
        if not force:
            cached = self.raster_path(page_no, dpi)
            if cached.exists() and cached.stat().st_size > 0:
                return cached
        path = self.render(page_no, dpi)
        self.enforce_raster_cache()
        return path

    def close(self) -> None:
        return None

    # ── raster helpers ──────────────────────────────────────────────────────

    def raster_path(self, page_no: int, dpi: int) -> Path:
        raster_dir = self.config.raster_dir / self.doc_id
        raster_dir.mkdir(parents=True, exist_ok=True)
        return raster_dir / f"p{page_no:05d}@{dpi}.png"

    @property
    def raster_bytes(self) -> int:
        raster_dir = self.config.raster_dir / self.doc_id
        if not raster_dir.is_dir():
            return 0
        return sum(f.stat().st_size for f in raster_dir.glob("*.png"))

    def enforce_raster_cache(self) -> None:
        """Keep the raster cache under its budget by deleting the oldest files.

        Rasters are the usual reason a large run exhausts a container, so the
        eviction happens here, next to the code that creates them, rather than
        being every operator's responsibility.
        """
        budget = int(getattr(self.config, "raster_cache_mb", 0)) * 1024 * 1024
        if budget <= 0:
            return
        raster_dir = self.config.raster_dir / self.doc_id
        if not raster_dir.is_dir():
            return
        files = sorted(raster_dir.glob("*.png"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        for path in files:
            if total <= budget:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue


def require_capability(plugin_caps: frozenset[Any], capability: Any, op: str) -> None:
    """Raise for a plugin operator that needs a capability it never declared."""
    if capability not in plugin_caps:
        raise CapabilityError(
            f"operator {op!r} needs the {getattr(capability, 'value', capability)!r} capability, "
            "which its plugin did not declare"
        )
