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
