"""Tests for ``POST /trace``: ordered crack cells with exact integer geometry.

Path expectations are never hard-coded from the production algorithm: an
independent per-cell intersection reference (exact ``fractions.Fraction``
arithmetic, completely separate from ``app.transform``) decides which cells
each segment enters, and the API result is compared against it on random
non-divisible grids and polylines. Fixed cases pin down the boundary
conventions (line walking, corner crossings, revisits, zero-length segments).
"""

from __future__ import annotations

import random
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

WIDTH, HEIGHT, ROWS, COLS = 600, 400, 4, 10


def trace(vertices, rotation=0, width=WIDTH, height=HEIGHT, rows=ROWS, cols=COLS):
    return client.post(
        "/trace",
        json={
            "width": width,
            "height": height,
            "rows": rows,
            "cols": cols,
            "rotation": rotation,
            "vertices": vertices,
        },
    )


def path_of(response):
    return [(point["row"], point["col"]) for point in response.json()["path"]]


# ---------------------------------------------------------------------------
# Independent reference implementation: per-cell region intersection.
#
# Strip k (1-based) owns the continuous half-open region
# [(k-1)*span/divisions, k*span/divisions); the last strip additionally
# closes on the outer edge. In a non-divisible layout the divider lines are
# fractional and cut through pixels. A point on a divider belongs to the
# right/lower strip, so a segment walking on a line stays on its right/lower
# side and, at a horizontal+vertical junction, only the diagonal cell owns
# the point (the side cells are merely touched and are never recorded).
# ---------------------------------------------------------------------------


def _ref_locate(u: int, v: int, cw: int, ch: int, rows: int, cols: int) -> tuple[int, int]:
    return (v * rows) // ch + 1, (u * cols) // cw + 1


def _ref_integer_line_pixels(span: int, divisions: int) -> list[int]:
    """Integer pixels strictly inside the canvas that lie on a divider line."""
    return [p for p in range(1, span) if p * divisions % span == 0]


def _ref_interior_strips(p0: int, p1: int, divisions: int, span: int) -> dict[int, tuple[Fraction, Fraction]]:
    """Strip -> parameter interval ``[lo, hi]`` restricted to ``(0, 1)``, ``lo < hi``."""
    delta = p1 - p0
    intervals: dict[int, tuple[Fraction, Fraction]] = {}
    if delta == 0:
        strip = (p0 * divisions) // span + 1
        intervals[strip] = (Fraction(0), Fraction(1))
        return intervals
    for strip in range(1, divisions + 1):
        lower = Fraction((strip - 1) * span - p0 * divisions, delta * divisions)
        upper = (
            Fraction(span - p0, delta)  # final strip closes on the outer edge
            if strip == divisions
            else Fraction(strip * span - p0 * divisions, delta * divisions)
        )
        lo = max(min(lower, upper), Fraction(0))
        hi = min(max(lower, upper), Fraction(1))
        if lo < hi:  # strictly positive-length ownership -> not a corner touch
            intervals[strip] = (lo, hi)
    return intervals


def ref_segment(u0, v0, u1, v1, cw, ch, rows, cols):
    """Ordered cells of one segment, including its start and endpoint cells."""
    x_intervals = _ref_interior_strips(u0, u1, cols, cw)
    y_intervals = _ref_interior_strips(v0, v1, rows, ch)
    entries = [(Fraction(-1), _ref_locate(u0, v0, cw, ch, rows, cols))]
    for row, (ylo, yhi) in y_intervals.items():
        for col, (xlo, xhi) in x_intervals.items():
            lo, hi = max(ylo, xlo), min(yhi, xhi)
            if lo < hi:
                entries.append((lo, (row, col)))
    entries.sort(key=lambda item: item[0])
    entries.append((Fraction(1), _ref_locate(u1, v1, cw, ch, rows, cols)))
    cells: list[tuple[int, int]] = []
    for _, cell in entries:
        if not cells or cells[-1] != cell:
            cells.append(cell)
    return cells


def ref_path(vertices, cw, ch, rows, cols):
    """Reference polyline path; shared segment junctions are emitted once."""
    path: list[tuple[int, int]] = []
    for (u0, v0), (u1, v1) in zip(vertices, vertices[1:]):
        if (u0, v0) == (u1, v1):  # zero-length segment creates no record
            continue
        segment = ref_segment(u0, v0, u1, v1, cw, ch, rows, cols)
        if path:
            segment = segment[1:]  # shared vertex already recorded
        for cell in segment:
            if not path or path[-1] != cell:
                path.append(cell)
    if not path:
        path = [_ref_locate(*vertices[0], cw, ch, rows, cols)]
    return path


def inverse_vertex(u, v, rotation, canvas_width, canvas_height):
    """Raw-image point whose normalization is ``(u, v)`` for the given rotation."""
    if rotation == 0:
        return u, v
    if rotation == 180:
        return canvas_width - 1 - u, canvas_height - 1 - v
    if rotation == 90:
        return v, canvas_width - 1 - u
    return canvas_height - 1 - v, u  # 270


def rotated_request_dims(rotation, canvas_width, canvas_height):
    if rotation in (90, 270):
        return canvas_height, canvas_width
    return canvas_width, canvas_height


# ---------------------------------------------------------------------------
# Response shape and normalization
# ---------------------------------------------------------------------------


def test_trace_response_shape_and_normalized_vertices():
    vertices = [{"x": 0, "y": 0}, {"x": 599, "y": 399}]
    response = trace(vertices, rotation=90)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rotation"] == 90
    assert body["canvas"] == {"width": HEIGHT, "height": WIDTH}
    assert body["grid"] == {"rows": ROWS, "cols": COLS}
    assert body["vertices"] == [{"x": 399, "y": 0}, {"x": 0, "y": 599}]
    path = path_of(response)
    assert path[0] == (1, 10) and path[-1] == (4, 1)
    for step in body["path"]:
        assert set(step) == {"row", "col"}


def test_single_vertex_returns_its_cell():
    response = trace([{"x": 60, "y": 100}])
    assert response.status_code == 200
    assert path_of(response) == [(2, 2)]


# ---------------------------------------------------------------------------
# Four orientations normalize the same physical polyline to the same path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size,rows,cols",
    [(400, 8, 4), (600, 10, 4), (333, 7, 5), (100, 6, 6), (1, 1, 1)],
)
def test_four_rotations_give_equivalent_paths(size, rows, cols):
    canvas_width = canvas_height = size
    normalized = [
        (0, 0),
        (size // 7, size // 13),
        (size - 1, size - 1),
        (size // 3, size // 2),
        (size // 2, size // 4),
    ]
    paths = {}
    for rotation in (0, 90, 180, 270):
        width, height = rotated_request_dims(rotation, canvas_width, canvas_height)
        raw_vertices = [
            {"x": x, "y": y}
            for x, y in (inverse_vertex(u, v, rotation, canvas_width, canvas_height) for u, v in normalized)
        ]
        response = trace(raw_vertices, rotation=rotation, width=width, height=height, rows=rows, cols=cols)
        assert response.status_code == 200, response.text
        body = response.json()
        assert [(p["x"], p["y"]) for p in body["vertices"]] == normalized
        assert (body["canvas"]["width"], body["canvas"]["height"]) == (canvas_width, canvas_height)
        paths[rotation] = path_of(response)
    assert paths[90] == paths[180] == paths[270] == paths[0]
    assert paths[0] == ref_path(normalized, canvas_width, canvas_height, rows, cols)


# ---------------------------------------------------------------------------
# Deterministic boundary-convention cases
# ---------------------------------------------------------------------------


def test_diagonal_corner_crossing_enters_diagonal_cell_only():
    response = trace([{"x": 0, "y": 0}, {"x": 60, "y": 100}])  # junction at (60,100)
    assert path_of(response) == [(1, 1), (2, 2)]  # (1,2)/(2,1) only touch the corner


def test_fractional_junction_corner_crossing():
    # 6x4 canvas, 4 cols (lines 1.5, 3, 4.5) and 2 rows (line 2):
    # (0,0)->(3,2) crosses x=1.5 alone, then reaches the x=3 / y=2 junction
    # together -- entering (2,3) diagonally; side cells (1,3)/(2,2) are
    # merely corner-touched and never recorded.
    response = trace([{"x": 0, "y": 0}, {"x": 3, "y": 2}], width=6, height=4, rows=2, cols=4)
    assert path_of(response) == [(1, 1), (1, 2), (2, 3)]


def test_walking_vertical_divider_belongs_to_right_column():
    response = trace([{"x": 60, "y": 0}, {"x": 60, "y": 200}])
    assert path_of(response) == [(1, 2), (2, 2), (3, 2)]


def test_walking_horizontal_divider_belongs_to_lower_row():
    response = trace([{"x": 0, "y": 100}, {"x": 200, "y": 100}])
    assert path_of(response) == [(2, 1), (2, 2), (2, 3), (2, 4)]


def test_walking_fractional_divider_pixel():
    # width=6, cols=4: divider lines at x = 1.5, 3, 4.5, so pixel 3 lies
    # exactly on the line between strips 2 and 3. Walking down that pixel stays
    # in the right-hand strip; turning left immediately leaves it.
    walk = trace([{"x": 3, "y": 0}, {"x": 3, "y": 7}], width=6, height=8, rows=4, cols=4)
    assert path_of(walk) == [(1, 3), (2, 3), (3, 3), (4, 3)]
    moving_off = trace([{"x": 3, "y": 0}, {"x": 2, "y": 0}], width=6, height=8, rows=4, cols=4)
    assert path_of(moving_off) == [(1, 3), (1, 2)]


def test_backtracking_revisit_is_kept_but_consecutive_duplicates_collapse():
    response = trace([{"x": 30, "y": 50}, {"x": 90, "y": 150}, {"x": 30, "y": 50}])
    assert path_of(response) == [(1, 1), (2, 2), (1, 1)]  # revisit after other cells kept


def test_repeated_vertices_are_zero_length_segments():
    once = trace([{"x": 10, "y": 10}, {"x": 90, "y": 150}])
    repeated = trace(
        [
            {"x": 10, "y": 10},
            {"x": 10, "y": 10},
            {"x": 90, "y": 150},
            {"x": 90, "y": 150},
            {"x": 90, "y": 150},
        ]
    )
    assert path_of(repeated) == path_of(once)


def test_uneven_grid_staircase_matches_reference():
    vertices = [(0, 0), (9, 6), (1, 5), (6, 0)]
    response = trace(
        [{"x": u, "y": v} for u, v in vertices], width=10, height=7, rows=3, cols=4
    )
    assert path_of(response) == ref_path(vertices, 10, 7, 3, 4)


# ---------------------------------------------------------------------------
# Randomized cross-check against the independent reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(60))
def test_random_polylines_match_reference(seed):
    rng = random.Random(seed)
    canvas_width = rng.randint(1, 40)
    canvas_height = rng.randint(1, 40)
    rows = rng.randint(1, 12)
    cols = rng.randint(1, 12)
    # Bias some vertices onto canvas edges and onto pixels that happen to lie
    # exactly on (possibly fractional) divider lines, to exercise line walking
    # and simultaneous horizontal/vertical junction crossings.
    x_line_pixels = _ref_integer_line_pixels(canvas_width, cols)
    y_line_pixels = _ref_integer_line_pixels(canvas_height, rows)

    def pick(line_pixels, span):
        if line_pixels and rng.random() < 0.3:
            return rng.choice(line_pixels)
        if rng.random() < 0.15:
            return rng.choice((0, span - 1))
        return rng.randrange(span)

    vertices = [
        (pick(x_line_pixels, canvas_width), pick(y_line_pixels, canvas_height))
        for _ in range(rng.randint(1, 7))
    ]
    rotation = rng.choice((0, 90, 180, 270))
    width, height = rotated_request_dims(rotation, canvas_width, canvas_height)
    raw_vertices = [
        {"x": x, "y": y}
        for x, y in (inverse_vertex(u, v, rotation, canvas_width, canvas_height) for u, v in vertices)
    ]
    response = trace(raw_vertices, rotation=rotation, width=width, height=height, rows=rows, cols=cols)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(p["x"], p["y"]) for p in body["vertices"]] == vertices
    assert path_of(response) == ref_path(vertices, canvas_width, canvas_height, rows, cols)


# ---------------------------------------------------------------------------
# Validation: batch-wide 422 with every offending input position
# ---------------------------------------------------------------------------


def test_out_of_bounds_vertex_rejects_batch_with_index():
    response = trace([{"x": 0, "y": 0}, {"x": WIDTH, "y": 5}, {"x": 1, "y": 1}])
    assert response.status_code == 422
    body = response.json()
    assert "path" not in body and "vertices" not in body
    assert any(error.get("index") == 1 for error in body["detail"])


def test_multiple_illegal_vertices_all_reported_by_input_position():
    bad = [
        {"x": 0, "y": 0},       # 0 ok
        {"x": WIDTH, "y": 0},   # 1 out of bounds
        {"x": -1, "y": 0},      # 2 out of bounds
        {"x": 1.5, "y": 0},     # 3 illegal type
        {"x": 0, "y": True},    # 4 booleans are not pixels
        {"x": 3},               # 5 malformed
        "not-a-vertex",         # 6 not an object
    ]
    response = trace(bad)
    assert response.status_code == 422
    indices = {error.get("index") for error in response.json()["detail"]}
    assert indices == {1, 2, 3, 4, 5, 6}


@pytest.mark.parametrize(
    "bad_vertex",
    [
        {"x": 1.0, "y": 0},
        {"x": "3", "y": 0},
        {"x": 0, "y": False},
        {"x": None, "y": 0},
        {"y": 0},
        {"x": 0},
        [0, 0],
        "vertex",
    ],
)
def test_illegal_vertex_type_rejected(bad_vertex):
    response = trace([bad_vertex])
    assert response.status_code == 422
    assert any(error.get("index") == 0 for error in response.json()["detail"])


def test_illegal_middle_vertex_rejects_whole_batch():
    response = trace([{"x": 1, "y": 1}, {"x": WIDTH, "y": HEIGHT}, {"x": 2, "y": 2}])
    assert response.status_code == 422
    assert any(error.get("index") == 1 for error in response.json()["detail"])


def test_empty_vertices_rejected():
    assert trace([]).status_code == 422


def test_rotation_bounds_checked_against_raw_canvas():
    # rotation 90 swaps dims: x must be < raw width (normalized height 400)
    response = trace([{"x": 400, "y": 0}], width=400, height=600, rotation=90)
    assert response.status_code == 422
    assert any(error.get("index") == 0 for error in response.json()["detail"])


def test_trace_invalid_rotation_and_dimensions_rejected():
    assert trace([{"x": 0, "y": 0}], rotation=45).status_code == 422
    assert trace([{"x": 0, "y": 0}], width=0).status_code == 422
    assert trace([{"x": 0, "y": 0}], rows=-2).status_code == 422


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


def test_inspect_and_health_still_work():
    response = client.post(
        "/inspect",
        json={
            "width": WIDTH,
            "height": HEIGHT,
            "rows": ROWS,
            "cols": COLS,
            "rotation": 0,
            "points": [{"x": 60, "y": 100}],
        },
    )
    assert response.status_code == 200
    (result,) = response.json()["results"]
    assert (result["row"], result["col"]) == (2, 2)
    assert client.get("/health").json() == {"status": "ok"}
