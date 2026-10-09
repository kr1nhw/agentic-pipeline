"""DeepSeek client and the budget that guards it.

Every wire detail here was verified against the live endpoint before being
written down, because the failure modes are silent:

* Images are sent as base64 data URLs. This was tested on a real page render.
* ``thinking`` is disabled by serialising ``{"type": "disabled"}``. The obvious
  alternative — ``reasoning_effort: "off"`` — is **rejected** by the API
  (``unknown variant 'off'``); the accepted values are
  none/minimal/low/medium/high/xhigh/ultra/max. Turning thinking off cut a
  classification from 118 to 11 completion tokens for the same answer.
* ``response_format: {"type": "json_object"}`` is honoured, but the model is not
  schema-bound: it will happily return ``"page_role": "academic paper"`` when
  asked for an enum. Every field is therefore validated on the way in.
* Reasoning tokens are billed and can dwarf the answer, so they are counted
  separately rather than folded into the completion total.
"""

from __future__ import annotations

import base64
import json
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

from docpipe.api.types import BudgetExceeded, DocpipeError

#: The API rejects "off"; the adapter-owned way to disable thinking is this.
THINKING_DISABLED: dict[str, str] = {"type": "disabled"}


class VLError(DocpipeError):
    """A model call could not be completed."""


@dataclass
class VLResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    usd: float = 0.0
    finish_reason: str | None = None
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


class TokenBucket:
    """Simple rate limiter. A 429 storm degrades a run; it must not storm one."""

    def __init__(self, rate_per_second: float, burst: float | None = None) -> None:
        self.rate = max(0.1, float(rate_per_second))
        self.capacity = float(burst if burst is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            time.sleep(min(wait, 5.0))


class Budget:
    """Hard stops, not warnings.

    Exhaustion is a clean, resumable stop: the remaining work stays queued and the
    run reports why it stopped, rather than quietly degrading to a worse pipeline.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.vl_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usd = 0.0
        self._lock = threading.Lock()
        self._per_doc: dict[str, int] = {}
        self.stopped_reason: str | None = None

    # ── checks ──────────────────────────────────────────────────────────────

    def check(self, doc_id: str | None = None, *, calls: int = 1) -> None:
        max_calls = int(getattr(self.config, "max_vl_calls_per_run", 0) or 0)
        max_tokens = int(getattr(self.config, "max_tokens_per_run", 0) or 0)
        max_usd = float(getattr(self.config, "max_usd", 0.0) or 0.0)
        max_doc = int(getattr(self.config, "max_vl_calls_per_doc", 0) or 0)

        with self._lock:
            if max_calls and self.vl_calls + calls > max_calls:
                self.stopped_reason = f"max_vl_calls_per_run ({max_calls}) reached"
            elif max_tokens and self.prompt_tokens + self.completion_tokens >= max_tokens:
                self.stopped_reason = f"max_tokens_per_run ({max_tokens}) reached"
            elif max_usd and self.usd >= max_usd:
                self.stopped_reason = f"max_usd ({max_usd}) reached"
            elif doc_id and max_doc and self._per_doc.get(doc_id, 0) + calls > max_doc:
                self.stopped_reason = f"max_vl_calls_per_doc ({max_doc}) reached for {doc_id}"
        if self.stopped_reason:
            raise BudgetExceeded(self.stopped_reason)

    @property
    def exhausted(self) -> bool:
        return self.stopped_reason is not None

    def charge(self, response: VLResponse, doc_id: str | None = None) -> None:
        with self._lock:
            self.vl_calls += 1
            self.prompt_tokens += response.prompt_tokens
            self.completion_tokens += response.completion_tokens
            self.usd += response.usd
            if doc_id:
                self._per_doc[doc_id] = self._per_doc.get(doc_id, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "vl_calls": self.vl_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "usd": round(self.usd, 6),
                "stopped_reason": self.stopped_reason,
            }


class VLClient:
    """Synchronous DeepSeek chat-completions client for the vision route."""

    def __init__(
        self,
        config: Any,
        budget: Budget | None = None,
        log: Any = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.budget = budget
        self.log = log or (lambda *args, **kwargs: None)
        self.model = getattr(config, "model", None)
        self.base_url = str(getattr(config, "base_url", "https://api.deepseek.com")).rstrip("/")
        self.timeout = float(getattr(config, "request_timeout_s", 180.0))
        self.max_retries = int(getattr(config, "max_retries", 3))
        self.bucket = TokenBucket(float(getattr(config, "rate_limit_rps", 4.0)))
        self._client = client or httpx.Client(timeout=self.timeout)
        self._owns_client = client is None
        self.total_calls = 0

    # ── lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "VLClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── credentials ─────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def api_key(self) -> str | None:
        import os

        key = getattr(self.config, "api_key", None) or os.environ.get("DEEPSEEK_API_KEY")
        return key.strip() if isinstance(key, str) and key.strip() else None

    # ── the call ────────────────────────────────────────────────────────────

    def complete(
        self,
        *,
        system: str | None = None,
        user: str,
        images: Iterable[Path | str] = (),
        model: str | None = None,
        thinking: bool = False,
        max_tokens: int = 2048,
        json_object: bool = False,
        temperature: float = 0.0,
        purpose: str = "extract",
        doc_id: str | None = None,
    ) -> VLResponse:
        if not self.configured:
            raise VLError(
                "no DeepSeek credential: set DEEPSEEK_API_KEY in the environment "
                "(the image never bakes a key)"
            )
        if self.budget is not None:
            self.budget.check(doc_id)

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_url(image)},
                }
            )
        messages.append({"role": "user", "content": content})

        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        if json_object:
            body["response_format"] = {"type": "json_object"}
        if not thinking:
            # Verified: `reasoning_effort: "off"` is rejected by this API.
            body["thinking"] = dict(THINKING_DISABLED)

        started = time.monotonic()
        response = self._post_with_retries(body, purpose=purpose)
        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = payload.get("usage") or {}
        completion_details = usage.get("completion_tokens_details") or {}

        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)
        usd = float(
            getattr(self.config, "price_for", lambda *args: 0.0)(prompt_tokens, completion_tokens)
            if hasattr(self.config, "price_for")
            else 0.0
        )

        text = message.get("content") or ""
        if not text and message.get("reasoning_content"):
            # A thinking model that spent its whole budget reasoning produced no
            # answer; say so instead of pretending the empty string is one.
            raise VLError(
                "model returned only reasoning content and no answer "
                f"(reasoning_tokens={reasoning_tokens}, finish={choice.get('finish_reason')}); "
                "raise max_tokens or disable thinking"
            )

        result = VLResponse(
            text=text,
            model=str(payload.get("model") or body["model"]),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            usd=usd,
            finish_reason=choice.get("finish_reason"),
            raw={"id": payload.get("id"), "usage": usage},
        )
        self.total_calls += 1
        if self.budget is not None:
            self.budget.charge(result, doc_id)
        self.log(
            "vl_call",
            purpose=purpose,
            model=result.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            seconds=round(time.monotonic() - started, 2),
        )
        return result

    # ── transport ───────────────────────────────────────────────────────────

    def _post_with_retries(self, body: dict[str, Any], *, purpose: str) -> httpx.Response:
        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.bucket.acquire()
            try:
                response = self._client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                last_error = VLError(f"transport failure talking to {self.base_url}: {exc}")
                self._sleep_backoff(attempt, None)
                continue

            if response.status_code == 200:
                return response

            if response.status_code in (401, 403):
                raise VLError(
                    f"DeepSeek rejected the credential (HTTP {response.status_code}); "
                    "check DEEPSEEK_API_KEY"
                )
            if response.status_code == 400:
                detail = _error_detail(response)
                if "context" in detail.lower() or "too long" in detail.lower():
                    raise VLError(f"request too large for the model: {detail}")
                raise VLError(f"invalid request ({detail})")
            if response.status_code == 429 or response.status_code >= 500:
                last_error = VLError(f"HTTP {response.status_code}: {_error_detail(response)}")
                self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                continue
            raise VLError(f"unexpected HTTP {response.status_code}: {_error_detail(response)}")

        raise last_error or VLError("model call failed after retries")

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                delay = min(60.0, float(retry_after))
                time.sleep(delay)
                return
            except (TypeError, ValueError):
                pass
        # Jittered exponential backoff: a corpus is many calls, and synchronised
        # retries are how you turn one 429 into a run-long outage.
        delay = min(30.0, 0.5 * (2 ** (attempt - 1)))
        time.sleep(delay * (0.5 + random.random()))


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return (response.text or "")[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(payload)[:300]


def _data_url(image: Path | str) -> str:
    path = Path(image)
    raw = path.read_bytes()
    suffix = path.suffix.lower().lstrip(".") or "png"
    media_type = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
        "bmp": "image/bmp",
    }.get(suffix, "image/png")
    return f"data:{media_type};base64,{base64.b64encode(raw).decode('ascii')}"
