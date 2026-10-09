"""Standalone image ingest.

An image is already a page: it needs no conversion, and its own pixels are the
raster a vision operator reads. The only work here is sizing and a cheap
ink/entropy profile, which is enough for the router to decide whether the image
is content (a chart, a diagram, a scan) or decoration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Collection, Any, Iterator

from docpipe.api.types import FormatContext, PageSignals, UnitSeed
from docpipe.ingest.base import BaseSession

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")

MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"BM": "image/bmp",
    b"II*\x00": "image/tiff",
    b"MM\x00*": "image/tiff",
}


def _magic_media_type(head: bytes) -> str | None:
    for magic, media_type in MAGIC.items():
        if head.startswith(magic):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class ImageSession(BaseSession):
    def __init__(self, path: Path, doc_id: str, config: Any, work_dir: Path) -> None:
        super().__init__(path=path, doc_id=doc_id, format_id="image", config=config, work_dir=work_dir)
        self._units = 1

    def close(self) -> None:
        return None

    def probe(self) -> dict[str, Any]:
        from PIL import Image

        with Image.open(self.path) as image:
            width, height = image.size
            mode = image.mode
            # An ink fraction from a downscaled luminance reduction: cheap, and
            # enough to separate a page scan from a logo.
            small = image.convert("L").resize((64, 64))
            pixels = list(small.getdata())
        dark = sum(1 for value in pixels if value < 200)
        return {
            "width": width,
            "height": height,
            "mode": mode,
            "ink_ratio": round(dark / len(pixels), 4),
            "aspect": round(width / height, 3) if height else None,
            "megapixels": round(width * height / 1e6, 2),
        }

    def enumerate_units(self, pages: Collection[int] | None = None) -> Iterator[UnitSeed]:
        if pages is not None and 1 not in {int(page) for page in pages}:
            return
        info = self.probe()
        signals = PageSignals(
            text_layer_chars=0,
            ink_ratio=info["ink_ratio"],
            page_width=float(info["width"]),
            page_height=float(info["height"]),
            image_area_frac=1.0,
            n_images=1,
            layout_hint="image",
        )
        yield UnitSeed(
            page_no=1,
            kind="image",
            signals=signals,
            text_layer=None,
            payload=info,
        )

    def deep_signals(self, page_no: int) -> dict[str, Any]:
        return self.probe()

    def render(self, page_no: int, dpi: int) -> Path:
        """The image is its own raster. ``dpi`` is ignored: there is no source
        resolution to re-render from, so upscaling would only add bytes."""
        return self.path


def match_image(head: bytes, path: str) -> float:
    if _magic_media_type(head):
        return 1.0
    return 0.8 if path.lower().endswith(IMAGE_EXTENSIONS) else 0.0


def open_image(path: Path, doc_id: str, config: Any) -> ImageSession:
    return ImageSession(path=path, doc_id=doc_id, config=config, work_dir=Path(config.work_dir))


def enumerate_image(ctx: FormatContext) -> Iterator[UnitSeed]:
    session = ImageSession(ctx.path, ctx.doc_id, ctx.scratch.get("config"), ctx.work_dir)
    yield from session.enumerate_units()
