"""Archetype routing: pick a static pipeline, or admit that none fits.

The router sees only free signals — format, text coverage, image area, layout
hint, table candidates, page count. It never renders anything and never calls a
model. That constraint is the point: for most documents the answer is "we already
know how to read this", and finding that answer must cost nothing.

When no archetype clears its confidence floor, the router says so, and the
document goes to the dynamic planner. The dossier handed to the planner carries
the near-misses, so the model usually confirms a candidate rather than inventing
a pipeline from nothing.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from typing import Any

from docpipe.schema import MatchRule, PageRecord, PipelineSpec

COMPARISON = re.compile(r"^\s*(<=|>=|==|!=|<|>)?\s*(-?\d+(?:\.\d+)?)\s*$")

OPERATORS = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


class RuleError(ValueError):
    """A match rule is malformed. Caught at catalog validation, not at run time."""


def parse_comparison(expression: str) -> tuple[Any, float]:
    match = COMPARISON.match(expression)
    if not match:
        raise RuleError(f"cannot parse comparison {expression!r} (expected e.g. '>=0.85')")
    symbol = match.group(1) or ">="
    return OPERATORS[symbol], float(match.group(2))


def validate_rule(rule: MatchRule) -> list[str]:
    """Check a rule without a document: the catalog validator's whole job."""
    problems: list[str] = []
    for field in ("text_layer_ratio", "image_area_frac", "pages"):
        expression = getattr(rule, field, None)
        if expression is None:
            continue
        try:
            parse_comparison(expression)
        except RuleError as exc:
            problems.append(f"{field}: {exc}")
    if not rule.format and not rule.layout_hint:
        problems.append("rule matches nothing: set at least 'format' or 'layout_hint'")
    return problems


def evaluate_rule(rule: MatchRule, *, format_id: str, signals: dict[str, Any], n_pages: int) -> float:
    """Return a confidence in 0..1. Zero means the rule rejects this document.

    Constraints are hard filters, and the confidence is how *specific* the match
    is: a rule that names the format and a layout hint is far more informative
    than one that merely accepts "pdf", and the router must prefer the specific
    rule when both fit.
    """
    if rule.format:
        formats = rule.format if isinstance(rule.format, list) else [rule.format]
        if format_id not in formats:
            return 0.0
    elif not rule.layout_hint:
        return 0.0

    confidence = 0.5
    checks = 0

    # A signal the probe did not compute is *unknown*, not violated. Rejecting on a
    # missing signal silently disqualified every text PDF whose image coverage had
    # not been measured yet, which then looked exactly like "no archetype fits".
    if rule.text_layer_ratio:
        op, threshold = parse_comparison(rule.text_layer_ratio)
        coverage = _text_coverage(signals)
        if coverage is not None:
            if not op(coverage, threshold):
                return 0.0
            checks += 1

    if rule.image_area_frac:
        op, threshold = parse_comparison(rule.image_area_frac)
        area = signals.get("image_area_frac")
        if area is not None:
            if not op(float(area), threshold):
                return 0.0
            checks += 1

    if rule.pages:
        op, threshold = parse_comparison(rule.pages)
        if not op(float(n_pages), threshold):
            return 0.0
        checks += 1

    if rule.layout_hint:
        # A layout hint is the most specific thing a rule can say, so it counts
        # double. Without that, a 16:9 PDF with a thin text layer ties between
        # "slide export" and "scanned image" and the alphabetically earlier
        # archetype wins — which is how a slide deck gets read as a scan.
        if signals.get("layout_hint") != rule.layout_hint:
            return 0.0
        checks += 2

    if rule.format:
        confidence += 0.15
    confidence += min(0.3, 0.15 * checks)
    return min(1.0, confidence)


def _text_coverage(signals: dict[str, Any]) -> float | None:
    """Characters per page area, normalised into a 0..1 coverage proxy.

    A raw character count means nothing across page sizes: 200 characters is a
    full slide and an empty A4. Normalising by area is what makes one threshold
    work for both.
    """
    chars = signals.get("text_layer_chars")
    width = signals.get("page_width")
    height = signals.get("page_height")
    if chars is None:
        return None
    if not width or not height:
        # No geometry: fall back to a page-size-agnostic saturating curve.
        return min(1.0, float(chars) / 3000.0)
    area = float(width) * float(height)
    if area <= 0:
        return min(1.0, float(chars) / 3000.0)
    density = float(chars) / area
    return max(0.0, min(1.0, density / 0.0025))


@dataclass
class RouteDecision:
    pipeline_id: str | None
    confidence: float
    candidates: list[tuple[str, float]]
    reason: str

    @property
    def confident(self) -> bool:
        return self.pipeline_id is not None


def route(
    pipelines: dict[str, PipelineSpec],
    *,
    format_id: str,
    signals: dict[str, Any],
    n_pages: int,
    min_confidence: float = 0.6,
) -> RouteDecision:
    """Score every archetype and return the winner, or an explicit miss."""
    scored: list[tuple[float, PipelineSpec]] = []
    for spec in pipelines.values():
        try:
            score = evaluate_rule(spec.match, format_id=format_id, signals=signals, n_pages=n_pages)
        except Exception:  # noqa: BLE001 - a broken rule must not take down routing
            score = 0.0
        if score > 0:
            scored.append((score, spec))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))

    candidates = [(spec.id, score) for score, spec in scored][:5]
    if not scored:
        return RouteDecision(None, 0.0, candidates, f"no archetype matches format {format_id!r}")

    score, spec = scored[0]
    floor = max(min_confidence, spec.match.min_confidence)
    if score < floor:
        return RouteDecision(
            None,
            score,
            candidates,
            f"best archetype {spec.id!r} scored {score:.2f} < floor {floor:.2f}",
        )
    return RouteDecision(spec.id, score, candidates, f"matched {spec.id!r} at {score:.2f}")


def route_document(
    pipelines: dict[str, PipelineSpec],
    *,
    format_id: str,
    pages: list[PageRecord],
    min_confidence: float = 0.6,
) -> RouteDecision:
    """Route a whole document from the aggregate of its pages.

    Aggregates are medians, not means: one full-page figure must not drag a
    text document into the scanned archetype.
    """
    if not pages:
        return RouteDecision(None, 0.0, [], "document has no units")
    signals = aggregate_signals(pages)
    return route(pipelines, format_id=format_id, signals=signals, n_pages=len(pages), min_confidence=min_confidence)


def aggregate_signals(pages: list[PageRecord]) -> dict[str, Any]:
    def median(values: list[float]) -> float | None:
        values = sorted(value for value in values if value is not None)
        if not values:
            return None
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2

    layout_hints: dict[str, int] = {}
    for page in pages:
        hint = page.signals.layout_hint
        if hint:
            layout_hints[hint] = layout_hints.get(hint, 0) + 1

    first = pages[0]
    return {
        "text_layer_chars": median([float(page.text_layer_chars) for page in pages]) or 0.0,
        "image_area_frac": median(
            [float(page.signals.image_area_frac) for page in pages if page.signals.image_area_frac is not None]
        ),
        "page_width": first.signals.page_width,
        "page_height": first.signals.page_height,
        "layout_hint": max(layout_hints.items(), key=lambda item: item[1])[0] if layout_hints else None,
        "n_table_candidates": sum(page.signals.n_table_candidates or 0 for page in pages),
        "units": len(pages),
    }
