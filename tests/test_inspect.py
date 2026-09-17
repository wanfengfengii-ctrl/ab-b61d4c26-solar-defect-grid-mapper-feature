"""Table-driven tests: four camera orientations, grid-line boundaries, batch rejection.

Reference canvas used by most cases: width=600, height=400, rows=4, cols=10.
Cell size on the 0/180 canvas is 60x100 px; on the 90/270 canvas (400x600) it
is 40x150 px, so every grid line lands on exact pixels.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

WIDTH, HEIGHT, ROWS, COLS = 600, 400, 4, 10


def inspect(points, rotation=0, width=WIDTH, height=HEIGHT, rows=ROWS, cols=COLS):
    return client.post(
        "/inspect",
        json={
            "width": width,
            "height": height,
            "rows": rows,
            "cols": cols,
            "rotation": rotation,
            "points": points,
        },
    )


# (rotation, raw (x, y), normalized (x, y), expected row, expected col)
ROTATION_TABLE = [
    # rotation 0: identity on a 600x400 canvas
    (0, (0, 0), (0, 0), 1, 1),
    (0, (59, 99), (59, 99), 1, 1),
    (0, (60, 100), (60, 100), 2, 2),  # on the grid lines -> right/bottom cell
    (0, (599, 399), (599, 399), 4, 10),
    # rotation 90: (x, y) -> (399 - y, x) on a 400x600 canvas
    (90, (0, 0), (399, 0), 1, 10),
    (90, (599, 399), (0, 599), 4, 1),
    (90, (0, 399), (0, 0), 1, 1),
    (90, (599, 0), (399, 599), 4, 10),
    (90, (150, 359), (40, 150), 2, 2),  # normalized point on both grid lines
    # rotation 180: (x, y) -> (599 - x, 399 - y) on a 600x400 canvas
    (180, (0, 0), (599, 399), 4, 10),
    (180, (599, 399), (0, 0), 1, 1),
    (180, (539, 299), (60, 100), 2, 2),  # normalized point on both grid lines
    # rotation 270: (x, y) -> (y, 599 - x) on a 400x600 canvas
    (270, (0, 0), (0, 599), 4, 1),
    (270, (599, 399), (399, 0), 1, 10),
    (270, (0, 399), (399, 599), 4, 10),
    (270, (599, 0), (0, 0), 1, 1),
    (270, (449, 40), (40, 150), 2, 2),  # normalized point on both grid lines
]


@pytest.mark.parametrize("rotation,point,normalized,row,col", ROTATION_TABLE)
def test_rotation_table(rotation, point, normalized, row, col):
    resp = inspect([{"x": point[0], "y": point[1]}], rotation=rotation)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    expected_canvas = (HEIGHT, WIDTH) if rotation in (90, 270) else (WIDTH, HEIGHT)
    assert (body["canvas"]["width"], body["canvas"]["height"]) == expected_canvas
    assert body["grid"] == {"rows": ROWS, "cols": COLS}
    (result,) = body["results"]
    assert result["index"] == 0
    assert (result["x"], result["y"]) == normalized
    assert (result["row"], result["col"]) == (row, col)


@pytest.mark.parametrize("k", range(1, COLS))
def test_vertical_grid_line_pixels_belong_to_right_cell(k):
    on_line = inspect([{"x": 60 * k, "y": 0}]).json()["results"][0]
    left_of_line = inspect([{"x": 60 * k - 1, "y": 0}]).json()["results"][0]
    assert on_line["col"] == k + 1
    assert left_of_line["col"] == k


@pytest.mark.parametrize("k", range(1, ROWS))
def test_horizontal_grid_line_pixels_belong_to_bottom_cell(k):
    on_line = inspect([{"x": 0, "y": 100 * k}]).json()["results"][0]
    above_line = inspect([{"x": 0, "y": 100 * k - 1}]).json()["results"][0]
    assert on_line["row"] == k + 1
    assert above_line["row"] == k


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_every_pixel_maps_to_exactly_one_valid_cell(rotation):
    """Dense sweep: each dark spot yields one determinate cell inside the grid."""
    points = [{"x": x, "y": y} for x in range(0, WIDTH, 7) for y in range(0, HEIGHT, 7)]
    resp = inspect(points, rotation=rotation)
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == len(points)
    canvas_w, canvas_h = (HEIGHT, WIDTH) if rotation in (90, 270) else (WIDTH, HEIGHT)
    for i, result in enumerate(results):
        assert result["index"] == i
        assert 0 <= result["x"] < canvas_w
        assert 0 <= result["y"] < canvas_h
        assert 1 <= result["row"] <= ROWS
        assert 1 <= result["col"] <= COLS


def test_results_preserve_input_order():
    points = [
        {"x": 599, "y": 399},
        {"x": 0, "y": 0},
        {"x": 60, "y": 100},
        {"x": 300, "y": 200},
        {"x": 61, "y": 100},
    ]
    results = inspect(points).json()["results"]
    assert [r["index"] for r in results] == list(range(len(points)))
    # rotation 0 keeps coordinates, so normalized (x, y) must echo the inputs
    assert [(r["x"], r["y"]) for r in results] == [(p["x"], p["y"]) for p in points]
    assert [(r["row"], r["col"]) for r in results] == [(4, 10), (1, 1), (2, 2), (3, 6), (2, 2)]


def test_uneven_grid_uses_floor_with_bottom_right_rule():
    # rows=6 over height=400 -> boundaries are not whole pixels; floor decides
    resp = inspect([{"x": 0, "y": 399}], rows=6)
    assert resp.json()["results"][0]["row"] == 6
    # y=200 -> floor(200*6/400)+1 = 4
    assert inspect([{"x": 0, "y": 200}], rows=6).json()["results"][0]["row"] == 4


@pytest.mark.parametrize(
    "field,value",
    [("width", 0), ("width", -1), ("height", 0), ("rows", 0), ("cols", -3)],
)
def test_non_positive_dimensions_rejected(field, value):
    kwargs = {"width": WIDTH, "height": HEIGHT, "rows": ROWS, "cols": COLS, field: value}
    assert inspect([{"x": 0, "y": 0}], **kwargs).status_code == 422


@pytest.mark.parametrize("rotation", [-90, 1, 45, 89, 360])
def test_invalid_rotation_rejected(rotation):
    assert inspect([{"x": 0, "y": 0}], rotation=rotation).status_code == 422


def test_empty_points_rejected():
    assert inspect([]).status_code == 422


@pytest.mark.parametrize(
    "bad_point",
    [
        {"x": -1, "y": 0},
        {"x": 0, "y": -1},
        {"x": WIDTH, "y": 0},  # x == width is out of bounds
        {"x": 0, "y": HEIGHT},  # y == height is out of bounds
        {"x": 10**6, "y": 10**6},
    ],
)
def test_out_of_bounds_point_rejects_whole_batch_with_index(bad_point):
    resp = inspect([{"x": 0, "y": 0}, bad_point, {"x": 1, "y": 1}])
    assert resp.status_code == 422
    body = resp.json()
    assert "results" not in body  # no partial results leak
    assert any(error.get("index") == 1 for error in body["detail"])


def test_invalid_batch_returns_no_partial_results():
    resp = inspect([{"x": 0, "y": 0}, {"x": 9999, "y": 0}])
    assert resp.status_code == 422
    assert "results" not in resp.json()


@pytest.mark.parametrize(
    "bad_point",
    [
        {"x": 1.5, "y": 0},  # fractional float is not an integer pixel
        {"x": "3", "y": 0},  # numeric string is not an integer
        {"x": 0, "y": True},  # booleans are not accepted as pixels
        {"x": 0},  # missing coordinate
        "not-a-point",
    ],
)
def test_non_integer_or_malformed_point_rejected(bad_point):
    resp = inspect([bad_point])
    assert resp.status_code == 422
    assert any(error.get("index") == 0 for error in resp.json()["detail"])


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
