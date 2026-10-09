"""Content-addressed operator cache.

The cache is what makes a re-run free rather than merely faster, and it is what
makes a sharded run cheap to restart. Its key is deliberately total: page
identity, the page's *content* hash, the operator, its parameters, and anything
else the operator says it depended on (backend, model). Change any of those and
the entry misses, which is the only safe behaviour.

A cache that keys on identity alone would serve yesterday's extraction for a file
that changed in place.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

CACHE_VERSION = "1"


def make_key(
    *,
    page_id: str,
    content_hash: str,
    op: str,
    params: dict[str, Any],
    extra: Any = (),
    version: str = CACHE_VERSION,
) -> str:
    payload = json.dumps(
        {
            "v": version,
            "page_id": page_id,
            "content": content_hash,
            "op": op,
            "params": params,
            "extra": list(extra) if isinstance(extra, (list, tuple)) else extra,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class OpCache:
    """JSON entries on disk, sharded by the first two key characters.

    Sharding matters at corpus scale: tens of thousands of entries in one
    directory makes every lookup an O(n) directory scan on some filesystems.
    """

    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return value if isinstance(value, dict) else None

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
            tmp.replace(path)
        except OSError:
            # A cache that cannot write is a cache miss, never a failed run.
            tmp.unlink(missing_ok=True)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses}
