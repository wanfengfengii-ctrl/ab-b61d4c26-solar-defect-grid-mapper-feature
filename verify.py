"""One-shot acceptance suite for the EL cell-locator API.

Runs as the ``verify`` service in docker-compose.yml: it waits for the API to
become healthy, executes real HTTP checks covering all four camera rotations,
grid-line boundary ownership, ordering and batch rejection, then exits 0 when
every check passed and 1 otherwise.
"""

from __future__ import annotations

import os
import random
import sys
import time
from fractions import Fraction
from typing import Any, Dict, List, Tuple

import httpx

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
STARTUP_TIMEOUT = float(os.environ.get("VERIFY_STARTUP_TIMEOUT", "60"))

WIDTH, HEIGHT, ROWS, COLS = 600, 400, 4, 10

passed = 0
failed: List[str] = []


def wait_for_api() -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while True:
        try:
            resp = httpx.get(f"{BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            print(f"FAIL  api not healthy at {BASE_URL} within {STARTUP_TIMEOUT:.0f}s")
            sys.exit(1)
        time.sleep(1.0)


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed.append(name)
        suffix = f"  -- {detail}" if detail else ""
        print(f"FAIL  {name}{suffix}")


def inspect(points: List[Dict[str, int]], rotation: int = 0, **overrides: Any) -> httpx.Response:
    payload: Dict[str, Any] = {
        "width": WIDTH,
        "height": HEIGHT,
        "rows": ROWS,
        "cols": COLS,
        "rotation": rotation,
        "points": points,
    }
    payload.update(overrides)
    return httpx.post(f"{BASE_URL}/inspect", json=payload, timeout=10.0)


# ---------------------------------------------------------------------------
# /trace acceptance
# ---------------------------------------------------------------------------

ZERO, ONE = Fraction(0), Fraction(1)


def _cell_bounds(length: int, count: int) -> List[Tuple[int, int, bool]]:
    return [
        (
            -((-(k - 1) * length) // count),
            -((-k * length) // count),
            k == count,  # canvas right/bottom edge is closed
        )
        for k in range(1, count + 1)
    ]


def _axis_interval(p0: int, p1: int, low: int, high: int, high_closed: bool):
    """Independent per-cell half-open slab intersection in parameter space."""
    d = p1 - p0
    lo, hi = ZERO, ONE

    def clip(lo2: Fraction, hi2: Fraction) -> bool:
        nonlocal lo, hi
        if lo2 > hi2:
            return False
        lo, hi = max(lo, lo2), min(hi, hi2)
        return lo <= hi

    if d == 0:
        if not (low <= p0 and (p0 < high or (high_closed and p0 == high))):
            return None
    else:
        t = Fraction(low - p0, d)
        if not (clip(t, ONE) if d > 0 else clip(ZERO, t)):
            return None
        # Half-open upper bound: a boundary parameter point alone is excluded
        # (unless it is the single closed canvas edge).
        t = Fraction(high - p0, d)
        if not (clip(ZERO, t) if d > 0 else clip(t, ONE)):
            return None
    return lo, hi


def _floor_cell(x: int, y: int, w: int, h: int, rows: int, cols: int) -> Tuple[int, int]:
    return ((y * rows) // h + 1, (x * cols) // w + 1)


def reference_trace(vertices: List[Tuple[int, int]], w: int, h: int, rows: int, cols: int):
    """Ordered cells from an independent per-cell intersection reference."""
    if not vertices:
        return []

    xb, yb = _cell_bounds(w, cols), _cell_bounds(h, rows)

    def segment_cells(x0, y0, x1, y1):
        if (x0, y0) == (x1, y1):
            return []
        spans = []
        for r, (yl, yh, yc) in enumerate(yb, start=1):
            for c, (xl, xh, xc) in enumerate(xb, start=1):
                ix = _axis_interval(x0, x1, xl, xh, xc)
                iy = _axis_interval(y0, y1, yl, yh, yc)
                if ix is None or iy is None:
                    continue
                lo, hi = max(ix[0], iy[0]), min(ix[1], iy[1])
                if lo < hi and ZERO <= lo and hi <= ONE:
                    spans.append((lo, (r, c)))
                # lo == hi at an interior t: only touches the cell corner.
        spans.sort(key=lambda z: (z[0], z[1]))
        ordered = [_floor_cell(x0, y0, w, h, rows, cols)]
        ordered.extend(cell for _, cell in spans)
        ordered.append(_floor_cell(x1, y1, w, h, rows, cols))
        return ordered

    seq = [_floor_cell(*vertices[0], w=w, h=h, rows=rows, cols=cols)]
    for (x0, y0), (x1, y1) in zip(vertices, vertices[1:]):
        seq.extend(segment_cells(x0, y0, x1, y1))
    out = []
    for cell in seq:
        if not out or out[-1] != cell:
            out.append(cell)
    return out


def trace(vertices: List[Dict[str, int]], rotation: int = 0, **overrides: Any) -> httpx.Response:
    payload: Dict[str, Any] = {
        "width": WIDTH,
        "height": HEIGHT,
        "rows": ROWS,
        "cols": COLS,
        "rotation": rotation,
        "vertices": vertices,
    }
    payload.update(overrides)
    return httpx.post(f"{BASE_URL}/trace", json=payload, timeout=10.0)


def _raw_from_normalized(u: int, v: int, w: int, h: int, rotation: int) -> Tuple[int, int]:
    if rotation == 0:
        return u, v
    if rotation == 90:
        return v, h - 1 - u
    if rotation == 180:
        return w - 1 - u, h - 1 - v
    return w - 1 - v, u  # 270


def check_trace() -> None:
    # Four orientations of one physical crack must give the same ordered path
    # on a square canvas/grid, verified against the independent reference.
    side, n = 40, 4
    normalized = [(2, 3), (20, 20), (38, 30), (10, 35), (20, 20)]
    expected = reference_trace(normalized, side, side, n, n)
    for rotation in (0, 90, 180, 270):
        raw = [
            {"x": rx, "y": ry}
            for rx, ry in (_raw_from_normalized(u, v, side, side, rotation) for u, v in normalized)
        ]
        resp = trace(raw, rotation=rotation, width=side, height=side, rows=n, cols=n)
        if resp.status_code != 200:
            check(f"trace rotation {rotation} accepted", False, resp.text[:120])
            continue
        body = resp.json()
        got = [(p["row"], p["col"]) for p in body["path"]]
        norm = [(p["x"], p["y"]) for p in body["vertices"]]
        check(
            f"trace rotation {rotation}: equivalent path vs reference",
            got == expected and norm == normalized,
            f"got {got}",
        )

    # Non-divisible grid with a diagonal corner crossing.
    resp = trace([{"x": 0, "y": 0}, {"x": 6, "y": 6}], width=10, height=10, rows=3, cols=3)
    got = [(p["row"], p["col"]) for p in resp.json()["path"]]
    exp = reference_trace([(0, 0), (6, 6)], 10, 10, 3, 3)
    check("trace non-divisible grid corner crossing", got == exp, f"got {got} exp {exp}")

    # Walking exactly on grid lines: attributed right/bottom.
    got = [(p["row"], p["col"]) for p in trace([{"x": 60, "y": 10}, {"x": 60, "y": 390}]).json()["path"]]
    check("trace along vertical grid line -> right column", got == [(1, 2), (2, 2), (3, 2), (4, 2)], f"got {got}")
    got = [(p["row"], p["col"]) for p in trace([{"x": 10, "y": 100}, {"x": 590, "y": 100}]).json()["path"]]
    check("trace along horizontal grid line -> bottom row", got == [(2, c) for c in range(1, 11)], f"got {got}")

    # Diagonal corner crossing: side cells omitted.
    got = [(p["row"], p["col"]) for p in trace([{"x": 59, "y": 99}, {"x": 61, "y": 101}]).json()["path"]]
    check("trace diagonal corner crossing skips side cells", got == [(1, 1), (2, 2)], f"got {got}")

    # Backtracking revisit + duplicate zero-length vertices.
    poly = [{"x": 10, "y": 10}, {"x": 590, "y": 390}, {"x": 10, "y": 10}]
    got = [(p["row"], p["col"]) for p in trace(poly).json()["path"]]
    exp = reference_trace([(10, 10), (590, 390), (10, 10)], WIDTH, HEIGHT, ROWS, COLS)
    check("trace backtracking keeps non-consecutive revisits", got == exp and got[0] == got[-1] == (1, 1), f"got {got}")

    dup = [{"x": 30, "y": 30}, {"x": 30, "y": 30}, {"x": 90, "y": 150}, {"x": 90, "y": 150}]
    got = [(p["row"], p["col"]) for p in trace(dup).json()["path"]]
    check("trace duplicate vertices add no records", got == [(1, 1), (1, 2), (2, 2)], f"got {got}")

    # Single vertex polyline: exactly its containing cell.
    body = trace([{"x": 60, "y": 100}]).json()
    check("trace single vertex returns its cell",
          [(p["row"], p["col"]) for p in body["path"]] == [(2, 2)]
          and [(p["x"], p["y"]) for p in body["vertices"]] == [(60, 100)])

    # Illegal middle vertex -> whole batch 422 naming every offending index.
    resp = trace([{"x": 0, "y": 0}, {"x": WIDTH, "y": 5}, {"x": 1, "y": 1}, {"x": "2", "y": 0}])
    body = resp.json()
    detail = body.get("detail", [])
    check("trace illegal vertices -> 422", resp.status_code == 422, f"status {resp.status_code}")
    check("trace rejected batch leaks no partial results", "path" not in body and "vertices" not in body)
    indices = sorted(err.get("index") for err in detail if err.get("index") is not None)
    check("trace 422 names all offending indices", indices == [1, 3], f"got {indices}")
    check("trace empty vertices -> 422", trace([]).status_code == 422)
    check("trace bad rotation -> 422", trace([{"x": 1, "y": 1}], rotation=45).status_code == 422)

    # Randomized differential checks against the independent reference.
    rng = random.Random(2026)
    configs = [(13, 7, 4, 5), (40, 60, 4, 10), (17, 31, 6, 3), (20, 11, 7, 4), (99, 101, 5, 7)]
    mismatch = ""
    for _ in range(120):
        w, h, rr, cc = rng.choice(configs)
        verts = [(rng.randrange(w), rng.randrange(h)) for _ in range(rng.randint(1, 6))]
        resp = trace([{"x": x, "y": y} for x, y in verts], width=w, height=h, rows=rr, cols=cc)
        got = [(p["row"], p["col"]) for p in resp.json()["path"]]
        exp = reference_trace(verts, w, h, rr, cc)
        if got != exp:
            mismatch = f"verts={verts} grid={rr}x{cc} got={got} exp={exp}"
            break
    check("trace 120 randomized polylines match per-cell reference", not mismatch, mismatch)


def check_rotation_table() -> None:
    # (rotation, raw point, normalized point, row, col) on the 600x400 / 4x10 grid
    table: List[Tuple[int, Tuple[int, int], Tuple[int, int], int, int]] = [
        (0, (0, 0), (0, 0), 1, 1),
        (0, (60, 100), (60, 100), 2, 2),
        (0, (599, 399), (599, 399), 4, 10),
        (90, (0, 0), (399, 0), 1, 10),
        (90, (599, 399), (0, 599), 4, 1),
        (90, (150, 359), (40, 150), 2, 2),
        (180, (0, 0), (599, 399), 4, 10),
        (180, (599, 399), (0, 0), 1, 1),
        (180, (539, 299), (60, 100), 2, 2),
        (270, (0, 0), (0, 599), 4, 1),
        (270, (599, 399), (399, 0), 1, 10),
        (270, (449, 40), (40, 150), 2, 2),
    ]
    for rotation, (x, y), (ex, ey), erow, ecol in table:
        resp = inspect([{"x": x, "y": y}], rotation=rotation)
        name = f"rotation {rotation}: ({x},{y}) -> ({ex},{ey}) cell ({erow},{ecol})"
        if resp.status_code != 200:
            check(name, False, f"status {resp.status_code}: {resp.text[:120]}")
            continue
        (result,) = resp.json()["results"]
        got = (result["x"], result["y"], result["row"], result["col"])
        check(name, got == (ex, ey, erow, ecol), f"got {got}")


def check_canvas_swap() -> None:
    for rotation, expected in [(0, (600, 400)), (90, (400, 600)), (180, (600, 400)), (270, (400, 600))]:
        resp = inspect([{"x": 0, "y": 0}], rotation=rotation)
        canvas = resp.json().get("canvas", {})
        got = (canvas.get("width"), canvas.get("height"))
        check(f"rotation {rotation}: normalized canvas {expected}", got == expected, f"got {got}")


def check_boundaries() -> None:
    # Vertical grid lines at x = 60k belong to the right-hand cell.
    for k in (1, 5, 9):
        col_on = inspect([{"x": 60 * k, "y": 0}]).json()["results"][0]["col"]
        col_left = inspect([{"x": 60 * k - 1, "y": 0}]).json()["results"][0]["col"]
        check(f"grid line x={60 * k} -> right cell", (col_on, col_left) == (k + 1, k),
              f"got on={col_on} left={col_left}")
    # Horizontal grid lines at y = 100k belong to the bottom cell.
    for k in (1, 2, 3):
        row_on = inspect([{"x": 0, "y": 100 * k}]).json()["results"][0]["row"]
        row_above = inspect([{"x": 0, "y": 100 * k - 1}]).json()["results"][0]["row"]
        check(f"grid line y={100 * k} -> bottom cell", (row_on, row_above) == (k + 1, k),
              f"got on={row_on} above={row_above}")


def check_ordering() -> None:
    points = [{"x": 599, "y": 399}, {"x": 0, "y": 0}, {"x": 300, "y": 200}, {"x": 60, "y": 100}]
    resp = inspect(points, rotation=90)
    results = resp.json()["results"]
    check(
        "results preserve input order",
        [r["index"] for r in results] == [0, 1, 2, 3],
        f"got {[r['index'] for r in results]}",
    )


def check_batch_rejection() -> None:
    resp = inspect([{"x": 0, "y": 0}, {"x": WIDTH, "y": 5}, {"x": 1, "y": 1}])
    body = resp.json()
    check("out-of-bounds batch -> 422", resp.status_code == 422, f"status {resp.status_code}")
    check("rejected batch leaks no partial results", "results" not in body)
    detail = body.get("detail", [])
    check(
        "422 names the offending array index",
        any(err.get("index") == 1 for err in detail),
        f"detail={detail}",
    )
    check("invalid rotation -> 422", inspect([{"x": 0, "y": 0}], rotation=45).status_code == 422)
    check("empty points -> 422", inspect([]).status_code == 422)
    check("non-positive width -> 422", inspect([{"x": 0, "y": 0}], width=0).status_code == 422)
    check("fractional pixel -> 422", inspect([{"x": 1.5, "y": 0}]).status_code == 422)


def main() -> int:
    wait_for_api()
    check_rotation_table()
    check_canvas_swap()
    check_boundaries()
    check_ordering()
    check_batch_rejection()
    check_trace()
    total = passed + len(failed)
    print(f"\n{passed}/{total} acceptance checks passed against {BASE_URL}")
    if failed:
        print("failed checks:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("ACCEPTANCE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
