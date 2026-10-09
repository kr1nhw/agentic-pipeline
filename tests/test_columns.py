"""Column geometry, tested against the page that defeated three earlier criteria.

The line geometry below is copied from a real harvested ACL paper
(`2023.acl-long.1.pdf`, page 2): columns at x = 71..290 and x = 306..526 with a
16 pt gutter. Every criterion that failed in production is reproduced here as a
case, so a future change cannot silently reintroduce one.
"""

from __future__ import annotations

from docpipe.ingest.pdf import assign_columns, detect_columns


def make_lines(spans: list[tuple[float, float]], text: str = "some words here") -> list[dict]:
    return [
        {"text": text, "x0": x0, "x1": x1, "top": 700.0 - index * 12, "bottom": 690.0 - index * 12}
        for index, (x0, x1) in enumerate(spans)
    ]


def two_column_page(*, crossing: int = 0, spanning: int = 0) -> list[dict]:
    left = [(71.0, 289.0)] * 20
    right = [(306.0, 526.0)] * 20
    # Overlong lines that start in the left column and end past the gutter.
    left[:crossing] = [(71.0, 305.0)] * crossing
    # Full-width elements: a title or a table caption.
    spans = [(71.0, 525.0)] * spanning + left + right
    return make_lines(spans)


def test_finds_the_gutter_on_a_two_column_page() -> None:
    boundary, evidence = detect_columns(two_column_page())
    assert boundary is not None
    assert evidence["columns"] == 2
    assert 289.0 <= boundary <= 306.0, f"boundary {boundary} is not in the gutter"
    assert evidence["left_lines"] == 20 and evidence["right_lines"] == 20


def test_a_full_width_caption_does_not_defeat_detection() -> None:
    """Criterion 1 failed here: one spanning caption filled the empty band."""
    boundary, evidence = detect_columns(two_column_page(spanning=3))
    assert boundary is not None and evidence["columns"] == 2


def test_a_few_overlong_lines_do_not_defeat_detection() -> None:
    """Criterion 3 failed here: the gutter was 80 % occupied, not empty."""
    boundary, evidence = detect_columns(two_column_page(crossing=4))
    assert boundary is not None, evidence
    assert evidence["columns"] == 2
    assert evidence["crossings"] <= 4


def test_indented_lines_inside_a_column_do_not_defeat_detection() -> None:
    """Criterion 2 failed here: varying line starts put the boundary mid-column."""
    spans = [(71.0, 289.0)] * 8 + [(110.0, 289.0)] * 8 + [(306.0, 526.0)] * 20
    boundary, evidence = detect_columns(make_lines(spans))
    assert boundary is not None, evidence
    assert 289.0 <= boundary <= 306.0, f"boundary {boundary} landed inside a column"


def test_a_single_column_page_reports_no_columns() -> None:
    boundary, evidence = detect_columns(make_lines([(71.0, 500.0)] * 30))
    assert boundary is None
    assert evidence["columns"] == 1


def test_a_page_with_too_few_lines_is_not_guessed_at() -> None:
    boundary, evidence = detect_columns(make_lines([(71.0, 289.0), (306.0, 526.0)]))
    assert boundary is None
    assert "lines" in evidence["reason"]


def test_lines_are_tagged_by_column() -> None:
    lines = two_column_page()
    assign_columns(lines, 297.0)
    assert {line["column"] for line in lines} == {1, 2}


def test_a_spanning_line_is_tagged_zero() -> None:
    lines = two_column_page(spanning=1)
    assign_columns(lines, 297.0)
    assert lines[0]["column"] == 0, "a full-width element must not be forced into a column"
