"""docpipe runtime configuration.

One settings object, resolved from the environment with the ``DOCPIPE_`` prefix,
so a container run is reproducible from its `docker run` arguments alone.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The image-capable route. Declared in presets/settings.yaml for the agent side
#: and here for the runtime side, because both must agree.
DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"
DEFAULT_BASE_URL = "https://api.deepseek.com"

#: A raster below this is refused for extraction: at 52 dpi the model invented a
#: page title that the text layer flatly contradicted.
MIN_EXTRACTION_DPI = 110
DEFAULT_TABLE_DPI = 150


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCPIPE_", extra="ignore")

    # ── paths ───────────────────────────────────────────────────────────────
    in_dir: Path = Path("/in")
    out_dir: Path = Path("/out")
    work_dir: Path = Path("/work")

    # ── model access ────────────────────────────────────────────────────────
    offline: bool = Field(
        default=False,
        description="Disable every vl-cost operator. A run that cannot succeed offline has a wrong plan.",
    )
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = Field(default=None, description="Falls back to DEEPSEEK_API_KEY.")
    request_timeout_s: float = 180.0
    max_retries: int = 3
    rate_limit_rps: float = Field(default=4.0, description="Token bucket for provider calls.")
    vl_workers: int = Field(default=8, description="Concurrent model calls (network-bound).")

    # ── model behaviour ─────────────────────────────────────────────────────
    planner_thinking: bool = Field(
        default=True,
        description="Thinking for the once-per-document plan. Quality matters more than tokens here.",
    )
    classify_thinking: bool = Field(
        default=False,
        description="Thinking for per-page classification. Off is ~10x cheaper and equally accurate.",
    )
    inspect_pages_k: int = Field(default=3, description="Page images the planner may look at per document.")
    audit_sample_rate: float = Field(
        default=0.05,
        description="Fraction of gate-passing pages re-checked, to measure the gate's recall.",
    )

    # ── quality ─────────────────────────────────────────────────────────────
    min_extraction_dpi: int = MIN_EXTRACTION_DPI
    table_dpi: int = DEFAULT_TABLE_DPI
    max_escalations: int = 2
    gate_text_floor_chars: int = 80
    gate_image_area_frac: float = 0.5
    #: Below this, the text layer is treated as unreliable and escalated. Set from
    #: measurement: an OCR layer scores 0.15, a born-digital paper 0.5-0.6.
    gate_trust_floor: float = 0.4

    # ── scale ───────────────────────────────────────────────────────────────
    workers: int = Field(default=0, description="Local-operator processes. 0 = half the CPUs.")
    vl_call_workers: int = 8
    pages_in_flight: int = 64
    raster_cache_mb: int = 2048
    max_rss_mb: int = 0
    unit_timeout_s: float = 300.0
    #: Wall-clock budget for a single deterministic operator call on one page.
    #: Cost here is not predictable from cheap signals, so it is bounded instead.
    op_time_budget_s: float = 6.0
    file_timeout_s: float = 3600.0
    max_file_size_gb: float = 4.0
    keep_rasters: bool = False
    window_pages: int = Field(default=200, description="Planning window for very large documents.")

    # ── budgets (hard stops, not warnings) ──────────────────────────────────
    max_vl_calls_per_run: int = 0
    max_tokens_per_run: int = 0
    max_usd: float = 0.0
    max_vl_calls_per_doc: int = 0
    price_prompt_per_mtok: float = 0.0
    price_completion_per_mtok: float = 0.0

    # ── execution ───────────────────────────────────────────────────────────
    engine: str = Field(default="local", description="local | daft | auto")
    resume: bool = False
    no_cache: bool = False
    shard: str | None = Field(default=None, description="'i/N' deterministic partition.")
    deterministic_only: bool = False

    # ── plugins ─────────────────────────────────────────────────────────────
    plugin_paths: list[Path] = Field(default_factory=list)
    load_entrypoint_plugins: bool = True

    # ── logging ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    quiet: bool = False

    @field_validator("plugin_paths", mode="before")
    @classmethod
    def _split_plugin_paths(cls, value: object) -> object:
        """Accept ``a:b`` from the environment as well as a list."""
        if isinstance(value, str):
            if not value.strip():
                return []
            return [Path(part) for part in value.split(":") if part.strip()]
        return value

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, value: str) -> str:
        allowed = {"local", "daft", "auto"}
        if value not in allowed:
            raise ValueError(f"engine must be one of {sorted(allowed)}, got {value!r}")
        if value == "daft":
            # Refuse rather than quietly fall back to the local executor: a run
            # that reports one engine and uses another is worse than an error.
            try:
                import daft  # noqa: F401
            except ImportError:
                raise ValueError(
                    "engine 'daft' needs the optional extra: pip install 'docpipe[daft]'. "
                    "The default image ships the local executor only."
                ) from None
        return value

    # ── derived ─────────────────────────────────────────────────────────────

    @property
    def effective_workers(self) -> int:
        if self.workers > 0:
            return self.workers
        import os

        return max(1, (os.cpu_count() or 2) // 2)

    @property
    def model_enabled(self) -> bool:
        return not self.offline and not self.deterministic_only

    @property
    def raster_dir(self) -> Path:
        return self.work_dir / "rasters"

    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache"

    def price_for(self, prompt_tokens: int, completion_tokens: int) -> float:
        """USD estimate. Zero unless prices are configured — an invented cost is
        worse than an absent one."""
        return (
            prompt_tokens / 1_000_000 * self.price_prompt_per_mtok
            + completion_tokens / 1_000_000 * self.price_completion_per_mtok
        )

    def ensure_dirs(self) -> None:
        for path in (self.out_dir, self.work_dir, self.raster_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_config(**overrides: object) -> Config:
    """Build a Config, preferring an explicit value over the environment."""
    return Config(**overrides)  # type: ignore[arg-type]
