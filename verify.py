"""One-shot acceptance suite for the EL cell-locator API.

Runs as the ``verify`` service in docker-compose.yml: it waits for the API to
become healthy, executes real HTTP checks covering all four camera rotations,
grid-line boundary ownership, ordering and batch rejection, then exits 0 when
every check passed and 1 otherwise.
"""

from __future__ import annotations

import os
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
# /trace: independent per-cell reference (exact Fraction arithmetic)
# ---------------------------------------------------------------------------


def _ref_strips(p0: int, p1: int, divisions: int, span: int) -> Dict[int, Tuple[Fraction, Fraction]]:
    delta = p1 - p0
    if delta == 0:
        return {(p0 * divisions) // span + 1: (Fraction(0), Fraction(1))}
    out: Dict[int, Tuple[Fraction, Fraction]] = {}
    for strip in range(1, divisions + 1):
        lower = Fraction((strip - 1) * span - p0 * divisions, delta * divisions)
        upper = (
            Fraction(span - p0, delta)
            if strip == divisions
            else Fraction(strip * span - p0 * divisions, delta * divisions)
        )
        lo = max(min(lower, upper), Fraction(0))
        hi = min(max(lower, upper), Fraction(1))
        if lo < hi:
            out[strip] = (lo, hi)
    return out


def reference_trace(vertices: List[Tuple[int, int]], cw: int, ch: int, rows: int, cols: int) -> List[Tuple[int, int]]:
    def locate(u: int, v: int) -> Tuple[int, int]:
        return (v * rows) // ch + 1, (u * cols) // cw + 1

    path: List[Tuple[int, int]] = []
    for (u0, v0), (u1, v1) in zip(vertices, vertices[1:]):
        if (u0, v0) == (u1, v1):
            continue
        xs = _ref_strips(u0, u1, cols, cw)
        ys = _ref_strips(v0, v1, rows, ch)
        entries = [(Fraction(-1), locate(u0, v0))]
        for row, (yl, yh) in ys.items():
            for col, (xl, xh) in xs.items():
                lo, hi = max(yl, xl), min(yh, xh)
                if lo < hi:
                    entries.append((lo, (row, col)))
        entries.sort(key=lambda item: item[0])
        entries.append((Fraction(1), locate(u1, v1)))
        segment = [cell for _, cell in entries]
        if path:
            segment = segment[1:]
        for cell in segment:
            if not path or path[-1] != cell:
                path.append(cell)
    if not path:
        path = [locate(*vertices[0])]
    return path


def trace(
    vertices: List[Dict[str, int]],
    rotation: int = 0,
    *,
    width: int = WIDTH,
    height: int = HEIGHT,
    rows: int = ROWS,
    cols: int = COLS,
) -> httpx.Response:
    return httpx.post(
        f"{BASE_URL}/trace",
        json={
            "width": width,
            "height": height,
            "rows": rows,
            "cols": cols,
            "rotation": rotation,
            "vertices": vertices,
        },
        timeout=10.0,
    )


def _inverse_vertex(u: int, v: int, rotation: int, size: int) -> Tuple[int, int]:
    if rotation == 0:
        return u, v
    if rotation == 180:
        return size - 1 - u, size - 1 - v
    if rotation == 90:
        return v, size - 1 - u
    return size - 1 - v, u


def check_trace() -> None:
    # Normalization + shape.
    resp = trace([{"x": 0, "y": 0}, {"x": 599, "y": 399}], rotation=270)
    check("trace: status 200", resp.status_code == 200, resp.text[:160])
    body = resp.json()
    check("trace: normalized canvas swapped", body["canvas"] == {"width": 400, "height": 600})
    check(
        "trace: normalized vertices echoed",
        body["vertices"] == [{"x": 0, "y": 599}, {"x": 399, "y": 0}],
        str(body["vertices"]),
    )

    # Single vertex -> its own cell.
    single = trace([{"x": 60, "y": 100}]).json()
    check(
        "trace: single vertex returns its cell",
        [(p["row"], p["col"]) for p in single["path"]] == [(2, 2)],
        str(single["path"]),
    )

    # Four orientations of the same physical polyline give the same path.
    normalized = [(10, 20), (70, 130), (399, 380), (300, 200), (50, 350)]
    rotated_paths: Dict[int, List[Tuple[int, int]]] = {}
    for rotation in (0, 90, 180, 270):
        raw = [
            {"x": x, "y": y}
            for x, y in (_inverse_vertex(u, v, rotation, 400) for u, v in normalized)
        ]
        r = trace(raw, rotation=rotation, width=400, height=400)
        check(f"trace rotation {rotation}: 200", r.status_code == 200, r.text[:120])
        data = r.json()
        check(
            f"trace rotation {rotation}: vertices normalized back",
            [(p["x"], p["y"]) for p in data["vertices"]] == normalized,
        )
        rotated_paths[rotation] = [(p["row"], p["col"]) for p in data["path"]]
    check(
        "trace: all four rotations yield the equivalent ordered path",
        rotated_paths[90] == rotated_paths[180] == rotated_paths[270] == rotated_paths[0],
        str(rotated_paths),
    )

    # Boundary conventions on the even grid.
    corner = trace([{"x": 0, "y": 0}, {"x": 60, "y": 100}]).json()
    check(
        "trace: junction crossing enters diagonal cell only",
        [(p["row"], p["col"]) for p in corner["path"]] == [(1, 1), (2, 2)],
        str(corner["path"]),
    )
    along_x = trace([{"x": 60, "y": 0}, {"x": 60, "y": 200}]).json()
    check(
        "trace: walking a vertical divider stays in the right column",
        [(p["row"], p["col"]) for p in along_x["path"]] == [(1, 2), (2, 2), (3, 2)],
        str(along_x["path"]),
    )
    along_y = trace([{"x": 0, "y": 100}, {"x": 200, "y": 100}]).json()
    check(
        "trace: walking a horizontal divider stays in the lower row",
        [(p["row"], p["col"]) for p in along_y["path"]] == [(2, 1), (2, 2), (2, 3), (2, 4)],
        str(along_y["path"]),
    )
    revisit = trace([{"x": 30, "y": 50}, {"x": 90, "y": 150}, {"x": 30, "y": 50}]).json()
    check(
        "trace: non-consecutive revisit kept, adjacent duplicates collapsed",
        [(p["row"], p["col"]) for p in revisit["path"]] == [(1, 1), (2, 2), (1, 1)],
        str(revisit["path"]),
    )
    once = trace([{"x": 10, "y": 10}, {"x": 90, "y": 150}]).json()
    repeated = trace(
        [
            {"x": 10, "y": 10},
            {"x": 10, "y": 10},
            {"x": 90, "y": 150},
            {"x": 90, "y": 150},
        ]
    ).json()
    check(
        "trace: repeated vertices are zero-length segments",
        [(p["row"], p["col"]) for p in repeated["path"]]
        == [(p["row"], p["col"]) for p in once["path"]],
    )

    # Non-divisible grids checked against the independent reference.
    uneven_cases = [
        (10, 7, 3, 4, [(0, 0), (9, 6), (1, 5), (6, 0)]),
        (6, 4, 2, 4, [(0, 0), (3, 2), (5, 3)]),
        (13, 9, 5, 7, [(1, 8), (12, 0), (6, 4), (0, 0), (7, 7)]),
    ]
    for cw, ch, rows, cols, verts in uneven_cases:
        r = trace([{"x": u, "y": v} for u, v in verts], width=cw, height=ch, rows=rows, cols=cols)
        got = [(p["row"], p["col"]) for p in r.json()["path"]]
        expected = reference_trace(verts, cw, ch, rows, cols)
        check(f"trace: non-divisible grid {cw}x{ch}/{rows}x{cols} matches cell reference", got == expected,
              f"got {got} expected {expected}")

    # Batch-wide 422 naming every offending vertex position.
    bad = trace(
        [
            {"x": 0, "y": 0},
            {"x": WIDTH, "y": 0},
            {"x": -1, "y": 1},
            {"x": 1.5, "y": 0},
            {"x": 3},
            "nope",
        ]
    )
    check("trace: illegal batch -> 422", bad.status_code == 422, f"status {bad.status_code}")
    check("trace: rejected batch leaks no path", "path" not in bad.json())
    indices = {err.get("index") for err in bad.json().get("detail", [])}
    check("trace: every bad vertex position reported", indices == {1, 2, 3, 4, 5}, str(indices))
    check("trace: empty vertices -> 422", trace([]).status_code == 422)


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
