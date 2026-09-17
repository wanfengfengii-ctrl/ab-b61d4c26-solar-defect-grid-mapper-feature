"""Pure integer geometry for EL image normalization and cell lookup.

The EL camera may be mounted in four orientations on the production line, so a
raw image can be rotated 0/90/180/270 degrees clockwise relative to the upright
module. All functions here operate on discrete pixel indices with exact integer
arithmetic; the origin is the top-left corner of the image.

Cell regions use a half-open interpretation ``[left, right) x [top, bottom)``
(the canvas right/bottom edges are closed). Grid lines therefore belong to the
cell on their right/bottom side, matching :func:`locate_cell`.
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import List, Sequence, Tuple

VALID_ROTATIONS = (0, 90, 180, 270)


def normalize_point(x: int, y: int, width: int, height: int, rotation: int) -> tuple[int, int]:
    """Map a raw camera pixel ``(x, y)`` to the upright (normalized) canvas.

    Clockwise mapping rules (origin top-left):

    * 0:   ``(x, y)``
    * 90:  ``(height - 1 - y, x)``
    * 180: ``(width - 1 - x, height - 1 - y)``
    * 270: ``(y, width - 1 - x)``
    """
    if rotation == 0:
        return x, y
    if rotation == 90:
        return height - 1 - y, x
    if rotation == 180:
        return width - 1 - x, height - 1 - y
    if rotation == 270:
        return y, width - 1 - x
    raise ValueError(f"unsupported rotation: {rotation!r} (expected one of {VALID_ROTATIONS})")


def normalized_canvas(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Canvas ``(width, height)`` after normalization.

    90 and 270 degrees swap the axes; 0 and 180 keep them.
    """
    if rotation in (90, 270):
        return height, width
    if rotation in (0, 180):
        return width, height
    raise ValueError(f"unsupported rotation: {rotation!r} (expected one of {VALID_ROTATIONS})")


def locate_cell(u: int, v: int, canvas_width: int, canvas_height: int, rows: int, cols: int) -> tuple[int, int]:
    """1-based ``(row, col)`` of the cell containing normalized pixel ``(u, v)``.

    ``row = floor(v * rows / canvas_height) + 1`` and likewise for ``col``.
    A pixel exactly on a grid line therefore belongs to the cell on the
    right / bottom side of the line, as required by the QC workflow.
    """
    row = (v * rows) // canvas_height + 1
    col = (u * cols) // canvas_width + 1
    return row, col


# ---------------------------------------------------------------------------
# Crack tracing: ordered sequence of cells crossed by a polyline.
#
# The walk is an exact integer grid traversal.  Interior cell boundaries are at
# ``b_k = ceil(k * length / count)`` which is consistent with locate_cell's
# floor buckets (the pixel ``b_k`` is the first pixel of the right/bottom
# cell).  Every intersection parameter is kept as a rational ``num / den`` and
# compared purely by cross multiplication -- no floats, hence no rounding.
#
# Conventions:
#
# * Cell regions are half-open ``[left, right) x [top, bottom)``; the canvas's
#   right and bottom edges are closed.  Interior grid lines belong to the cell
#   on their right/bottom side, exactly as in :func:`locate_cell`.
# * When a segment crosses a horizontal and a vertical boundary at the same
#   parameter (a grid corner), it steps diagonally straight into the opposite
#   cell; the side cells the segment only touches at that corner are omitted.
# * A segment lying on a grid line is attributed to the right/bottom cells.
# * Endpoints are marked crack points: the cell of the vertex itself is always
#   recorded (via :func:`locate_cell`), including when the segment immediately
#   leaves a junction diagonally.
# ---------------------------------------------------------------------------

# Event deltas: entering/leaving along rows (dr) and columns (dc) at the same
# intersection parameter.  Coincident events (same rational t) are summed, so a
# corner crossing naturally becomes one diagonal step.
_CrossEvent = Tuple[int, int, int, int]  # (num, den, dr, dc), t = num / den


def _boundaries(count: int, length: int) -> List[int]:
    """Interior cell-boundary pixel positions ``ceil(k * length / count)``."""
    return [-((-k * length) // count) for k in range(1, count)]


def _cmp_event(a: _CrossEvent, b: _CrossEvent) -> int:
    """Order two rational parameters ``num/den`` by cross multiplication."""
    lhs = a[0] * b[1]
    rhs = b[0] * a[1]
    if lhs < rhs:
        return -1
    if lhs > rhs:
        return 1
    return 0


def trace_polyline(
    vertices: Sequence[Tuple[int, int]],
    canvas_width: int,
    canvas_height: int,
    rows: int,
    cols: int,
) -> List[Tuple[int, int]]:
    """Ordered 1-based ``(row, col)`` cells entered by the polyline.

    ``vertices`` are already normalized to the upright canvas.  Consecutive
    duplicate vertices are zero-length segments and add no records; a later
    revisit of a cell is kept, while the same address produced at the junction
    of consecutive segments is recorded only once.
    """
    if not vertices:
        return []

    v_lines = _boundaries(cols, canvas_width)
    h_lines = _boundaries(rows, canvas_height)

    def cell_of(x: int, y: int) -> Tuple[int, int]:
        return locate_cell(x, y, canvas_width, canvas_height, rows, cols)

    row, col = cell_of(*vertices[0])
    path: List[Tuple[int, int]] = [(row, col)]

    def enter(new_row: int, new_col: int) -> None:
        nonlocal row, col
        if (new_row, new_col) != (row, col):
            row, col = new_row, new_col
            path.append((row, col))

    for (x0, y0), (x1, y1) in zip(vertices, vertices[1:]):
        dx, dy = x1 - x0, y1 - y0
        if dx == 0 and dy == 0:
            # Zero-length segment: no movement at all, no extra record.
            continue

        # Departure at t = 0.  The start vertex is attributed right/bottom
        # (already current).  Moving left/up off a boundary enters the other
        # side immediately; both axes at once is a diagonal departure.
        dc_start = -sum(1 for b in v_lines if b == x0) if dx < 0 else 0
        dr_start = -sum(1 for b in h_lines if b == y0) if dy < 0 else 0
        if dr_start or dc_start:
            enter(row + dr_start, col + dc_start)

        # Strictly interior crossings, 0 < t < 1.
        events: List[_CrossEvent] = []
        if dx > 0:
            events.extend((b - x0, dx, 0, 1) for b in v_lines if x0 < b < x1)
        elif dx < 0:
            events.extend((x0 - b, -dx, 0, -1) for b in v_lines if x1 < b < x0)
        if dy > 0:
            events.extend((b - y0, dy, 1, 0) for b in h_lines if y0 < b < y1)
        elif dy < 0:
            events.extend((y0 - b, -dy, -1, 0) for b in h_lines if y1 < b < y0)

        events.sort(key=cmp_to_key(_cmp_event))

        # Group crossings at identical parameters; simultaneous row and column
        # crossings collapse into a single diagonal entry.
        index = 0
        while index < len(events):
            num, den, dr, dc = events[index]
            j = index + 1
            while j < len(events) and events[j][0] * den == num * events[j][1]:
                dr += events[j][2]
                dc += events[j][3]
                j += 1
            enter(row + dr, col + dc)
            index = j

        # Arrival at t = 1: the marked endpoint uses right/bottom attribution,
        # which can differ from the open-interval cell (diagonal arrivals at a
        # junction land directly in the attributed cell, skipping side cells).
        enter(*cell_of(x1, y1))

    return path
