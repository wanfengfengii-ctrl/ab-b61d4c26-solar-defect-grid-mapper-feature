"""Acceptance tests for POST /trace.

The expected path is never hard-coded from the production implementation: an
independent oracle below computes, for every cell separately, the exact
parameter interval of each segment intersecting the half-open cell region
(``fractions.Fraction`` Liang-Barsky clipping). A cell that only touches the
segment at a single corner point is rejected, and results are cross-checked
against the service over real HTTP calls.

Coverage: the four camera orientations on one polyline, non-divisible grids,
walking exactly on grid lines, diagonal corner crossings, backtracking /
revisits, duplicate zero-length vertices, single-vertex polylines and illegal
middle vertices (batch 422 with every offending index).
"""

from __future__ import annotations

import random
from fractions import Fraction
from itertools import product
from typing import List, Sequence, Tuple

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

WIDTH, HEIGHT, ROWS, COLS = 600, 400, 4, 10
Cell = Tuple[int, int]


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


# --------------------------------------------------------------------------
# Independent reference oracle (exact rational per-cell intersection).
# --------------------------------------------------------------------------

ZERO, ONE = Fraction(0), Fraction(1)


def _cell_bounds(length: int, count: int):
    return [
        (
            -((-(k - 1) * length) // count),  # ceil((k-1)*length/count)
            -((-k * length) // count),         # ceil(k*length/count)
            k == count,                        # canvas edge is closed
        )
        for k in range(1, count + 1)
    ]


def _axis_interval(p0, p1, low, high, high_closed):
    """t-subset of [0,1] for which low <= p(t) < high (<= at canvas edge)."""
    d = p1 - p0
    lo, hi, lo_in, hi_in = ZERO, ONE, True, True

    def clip(lo2, hi2, lo2_in=True, hi2_in=True):
        nonlocal lo, hi, lo_in, hi_in
        if lo2 > hi2:
            return False
        if lo2 > lo:
            lo, lo_in = lo2, lo2_in
        elif lo2 == lo:
            lo_in = lo_in and lo2_in
        if hi2 < hi:
            hi, hi_in = hi2, hi2_in
        elif hi2 == hi:
            hi_in = hi_in and hi2_in
        return lo <= hi

    if d == 0:
        if not (low <= p0 and (p0 < high or (high_closed and p0 == high))):
            return None
    else:
        t = Fraction(low - p0, d)
        if not (clip(t, ONE) if d > 0 else clip(ZERO, t)):
            return None
        t = Fraction(high - p0, d)
        if not (clip(ZERO, t, True, high_closed) if d > 0 else clip(t, ONE, high_closed, True)):
            return None
    return lo, hi, lo_in, hi_in


def _floor_cell(x, y, w, h, rows, cols) -> Cell:
    return ((y * rows) // h + 1, (x * cols) // w + 1)


def _reference_segment(x0, y0, x1, y1, w, h, rows, cols) -> List[Cell]:
    xb, yb = _cell_bounds(w, cols), _cell_bounds(h, rows)
    spans: List[Tuple[Fraction, Cell]] = []
    for r, (yl, yh, yc) in enumerate(yb, start=1):
        for c, (xl, xh, xc) in enumerate(xb, start=1):
            ix = _axis_interval(x0, x1, xl, xh, xc)
            iy = _axis_interval(y0, y1, yl, yh, yc)
            if ix is None or iy is None:
                continue
            lo = max(ix[0], iy[0])
            hi = min(ix[1], iy[1])
            if 0 <= lo <= 1 and 0 <= hi <= 1 and lo < hi:
                spans.append((lo, (r, c)))
            # lo == hi at an interior parameter: mere corner touch -> skip.
    spans.sort(key=lambda z: (z[0], z[1]))
    ordered: List[Cell] = [_floor_cell(x0, y0, w, h, rows, cols)]
    ordered.extend(cell for _, cell in spans)
    ordered.append(_floor_cell(x1, y1, w, h, rows, cols))
    return ordered


def _collapse(cells: Sequence[Cell]) -> List[Cell]:
    out: List[Cell] = []
    for cell in cells:
        if not out or out[-1] != cell:
            out.append(cell)
    return out


def reference_trace(vertices, w, h, rows, cols) -> List[Cell]:
    if not vertices:
        return []
    seq: List[Cell] = [_floor_cell(*vertices[0], w, h, rows, cols)]
    for (x0, y0), (x1, y1) in zip(vertices, vertices[1:]):
        if (x0, y0) == (x1, y1):
            continue  # zero-length segment adds no record
        seq.extend(_reference_segment(x0, y0, x1, y1, w, h, rows, cols))
    return _collapse(seq)


def _raw_from_normalized(u, v, w, h, rotation):
    """Inverse of the documented clockwise normalization formulas."""
    if rotation == 0:
        return u, v
    if rotation == 90:
        return v, h - 1 - u
    if rotation == 180:
        return w - 1 - u, h - 1 - v
    if rotation == 270:
        return w - 1 - v, u
    raise AssertionError(rotation)


# --------------------------------------------------------------------------
# Exhaustive small-grid differential tests.
# --------------------------------------------------------------------------

SMALL_CONFIGS = [
    (5, 5, 2, 2), (6, 4, 3, 2), (7, 5, 2, 3), (10, 10, 3, 3), (4, 7, 4, 2),
]


@pytest.mark.parametrize("w,h,rows,cols", SMALL_CONFIGS)
def test_all_single_segments_match_reference(w, h, rows, cols):
    for (x0, y0), (x1, y1) in product(
        [(x, y) for x in range(w) for y in range(h)], repeat=2
    ):
        resp = trace(
            [{"x": x0, "y": y0}, {"x": x1, "y": y1}],
            width=w, height=h, rows=rows, cols=cols,
        )
        assert resp.status_code == 200, resp.text
        got = [(p["row"], p["col"]) for p in resp.json()["path"]]
        assert got == reference_trace([(x0, y0), (x1, y1)], w, h, rows, cols)


@pytest.mark.parametrize("w,h,rows,cols", [(4, 4, 2, 2), (5, 3, 3, 2)])
def test_all_three_vertex_polylines_match_reference(w, h, rows, cols):
    pts = [(x, y) for x in range(w) for y in range(h)]
    for a, b, c in product(pts, repeat=3):
        resp = trace(
            [{"x": a[0], "y": a[1]}, {"x": b[0], "y": b[1]}, {"x": c[0], "y": c[1]}],
            width=w, height=h, rows=rows, cols=cols,
        )
        got = [(p["row"], p["col"]) for p in resp.json()["path"]]
        assert got == reference_trace([a, b, c], w, h, rows, cols)


# --------------------------------------------------------------------------
# Randomized differential tests: non-divisible grids, fuzzed polylines.
# --------------------------------------------------------------------------

FUZZ_CONFIGS = [
    (13, 7, 4, 5), (40, 60, 4, 10), (17, 31, 6, 3), (3, 3, 1, 1),
    (20, 11, 7, 4), (600, 400, 4, 10), (99, 101, 5, 7),
]


@pytest.mark.parametrize("seed", range(20))
def test_fuzzed_polylines_match_reference(seed):
    rng = random.Random(seed)
    for _ in range(300):
        w, h, rows, cols = rng.choice(FUZZ_CONFIGS)
        nvert = rng.randint(1, 6)
        verts = [(rng.randrange(w), rng.randrange(h)) for _ in range(nvert)]
        resp = trace(
            [{"x": x, "y": y} for x, y in verts],
            width=w, height=h, rows=rows, cols=cols,
        )
        assert resp.status_code == 200, resp.text
        got = [(p["row"], p["col"]) for p in resp.json()["path"]]
        assert got == reference_trace(verts, w, h, rows, cols)


# --------------------------------------------------------------------------
# Four camera orientations on the same physical crack polyline.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_four_rotations_produce_equivalent_path(rotation):
    # Square canvas and square grid so axis swaps do not relabel the grid.
    side, n = 40, 4
    normalized = [(2, 3), (20, 20), (38, 30), (10, 35), (20, 20)]
    raw = [
        {"x": rx, "y": ry}
        for rx, ry in (_raw_from_normalized(u, v, side, side, rotation) for u, v in normalized)
    ]
    resp = trace(raw, rotation=rotation, width=side, height=side, rows=n, cols=n)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["canvas"]["width"], body["canvas"]["height"]) == (side, side)
    assert [(p["x"], p["y"]) for p in body["vertices"]] == normalized
    expected = reference_trace(normalized, side, side, n, n)
    assert [(p["row"], p["col"]) for p in body["path"]] == expected


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotations_agree_with_each_other_on_diagonal_crack(rotation):
    side, n = 60, 6
    normalized = [(0, 0), (30, 30), (59, 59)]  # crosses grid corners diagonally
    raw = [
        {"x": rx, "y": ry}
        for rx, ry in (_raw_from_normalized(u, v, side, side, rotation) for u, v in normalized)
    ]
    body = trace(raw, rotation=rotation, width=side, height=side, rows=n, cols=n).json()
    # Every crossing is simultaneous row+column: a pure diagonal (k,k) walk.
    assert [(p["row"], p["col"]) for p in body["path"]] == [(k, k) for k in range(1, 7)]


# --------------------------------------------------------------------------
# Required geometric edge cases.
# --------------------------------------------------------------------------

def test_diagonal_corner_crossing_skips_side_cells():
    body = trace([{"x": 59, "y": 99}, {"x": 61, "y": 101}]).json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [(1, 1), (2, 2)]


def test_arrival_exactly_at_corner_lands_in_diagonal_cell():
    body = trace([{"x": 50, "y": 90}, {"x": 60, "y": 100}]).json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [(1, 1), (2, 2)]


def test_departure_from_corner_moving_opposite_diagonal():
    body = trace([{"x": 60, "y": 100}, {"x": 50, "y": 90}]).json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [(2, 2), (1, 1)]


def test_walk_along_vertical_grid_line_owned_by_right_column():
    body = trace([{"x": 60, "y": 10}, {"x": 60, "y": 390}]).json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [
        (1, 2), (2, 2), (3, 2), (4, 2)
    ]


def test_walk_along_horizontal_grid_line_owned_by_bottom_row():
    body = trace([{"x": 10, "y": 100}, {"x": 590, "y": 100}]).json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [(2, c) for c in range(1, 11)]


def test_walk_backwards_along_grid_line():
    body = trace([{"x": 590, "y": 100}, {"x": 10, "y": 100}]).json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [(2, c) for c in range(10, 0, -1)]


def test_backtracking_revisits_are_kept():
    body = trace([{"x": 10, "y": 10}, {"x": 590, "y": 390}, {"x": 10, "y": 10}]).json()
    path = [(p["row"], p["col"]) for p in body["path"]]
    assert path[0] == path[-1] == (1, 1)
    assert (4, 10) in path
    # non-consecutive revisit: cells recur after having been left
    assert path.count((2, 2)) >= 1 or path.count((1, 2)) >= 1
    assert path == reference_trace([(10, 10), (590, 390), (10, 10)], 600, 400, 4, 10)


def test_duplicate_vertices_are_zero_length_and_add_no_records():
    body = trace([
        {"x": 30, "y": 30}, {"x": 30, "y": 30},
        {"x": 90, "y": 150}, {"x": 90, "y": 150}, {"x": 90, "y": 150},
    ]).json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [(1, 1), (1, 2), (2, 2)]
    assert len(body["vertices"]) == 5


def test_junction_cell_not_duplicated_between_segments():
    # Two segments meeting inside one cell must list that cell only once.
    body = trace([{"x": 5, "y": 5}, {"x": 10, "y": 10}, {"x": 20, "y": 20}]).json()
    path = [(p["row"], p["col"]) for p in body["path"]]
    assert path[0] == (1, 1)
    assert path.count((1, 1)) == 1


def test_single_vertex_returns_its_cell():
    resp = trace([{"x": 60, "y": 100}])
    body = resp.json()
    assert [(p["row"], p["col"]) for p in body["path"]] == [(2, 2)]
    assert [(p["x"], p["y"]) for p in body["vertices"]] == [(60, 100)]


def test_response_contains_normalized_vertices_in_order():
    resp = trace([{"x": 0, "y": 0}, {"x": 599, "y": 399}], rotation=90)
    body = resp.json()
    assert body["rotation"] == 90
    assert body["canvas"] == {"width": 400, "height": 600}
    assert body["grid"] == {"rows": 4, "cols": 10}
    assert [(p["index"], p["x"], p["y"]) for p in body["vertices"]] == [
        (0, 399, 0), (1, 0, 599)
    ]


# --------------------------------------------------------------------------
# Validation: batch rejection with all offending indices, no partial output.
# --------------------------------------------------------------------------

def test_empty_vertices_rejected():
    assert trace([]).status_code == 422


@pytest.mark.parametrize("bad", [
    {"x": -1, "y": 0},
    {"x": WIDTH, "y": 0},
    {"x": 0, "y": HEIGHT},
])
def test_illegal_middle_vertex_rejects_batch(bad):
    vertices = [{"x": 1, "y": 1}, bad, {"x": 2, "y": 2}]
    resp = trace(vertices)
    assert resp.status_code == 422
    body = resp.json()
    assert "path" not in body and "vertices" not in body
    assert any(err.get("index") == 1 for err in body["detail"])


def test_multiple_illegal_vertices_all_reported_in_input_order():
    vertices = [
        {"x": WIDTH, "y": 0},   # 0: out of bounds
        {"x": "1", "y": 0},     # 1: wrong type
        {"x": 0, "y": -1},      # 2: out of bounds
        {"x": 1.5, "y": 0},     # 3: float
        {"x": True, "y": 0},    # 4: bool is not an int
        {"x": 1, "y": 2},       # 5: legal
        {"y": 3},               # 6: missing x
    ]
    resp = trace(vertices)
    assert resp.status_code == 422
    indices = sorted(err["index"] for err in resp.json()["detail"] if "index" in err)
    assert indices == [0, 1, 2, 3, 4, 6]


def test_non_integer_canvas_fields_rejected():
    for field, value in [("width", 1.5), ("rows", "4"), ("height", True)]:
        payload = {
            "width": WIDTH, "height": HEIGHT, "rows": ROWS, "cols": COLS,
            "rotation": 0, "vertices": [{"x": 1, "y": 1}], field: value,
        }
        resp = client.post("/trace", json=payload)
        assert resp.status_code == 422


@pytest.mark.parametrize("rotation", [-90, 45, 360])
def test_invalid_rotation_rejected(rotation):
    assert trace([{"x": 1, "y": 1}], rotation=rotation).status_code == 422


def test_health_unchanged():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_inspect_contract_unchanged():
    resp = client.post(
        "/inspect",
        json={
            "width": WIDTH, "height": HEIGHT, "rows": ROWS, "cols": COLS,
            "rotation": 90,
            "points": [{"x": 0, "y": 0}, {"x": 599, "y": 399}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"rotation", "canvas", "grid", "results"}
    assert body["results"][0] == {"index": 0, "x": 399, "y": 0, "row": 1, "col": 10}
    assert body["results"][1] == {"index": 1, "x": 0, "y": 599, "row": 4, "col": 1}
