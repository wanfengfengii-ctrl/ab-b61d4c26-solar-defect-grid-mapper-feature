"""Pure integer geometry for EL image normalization and cell lookup.

The EL camera may be mounted in four orientations on the production line, so a
raw image can be rotated 0/90/180/270 degrees clockwise relative to the upright
module. All functions here operate on discrete pixel indices with exact integer
arithmetic; the origin is the top-left corner of the image.
"""

from __future__ import annotations

from functools import cmp_to_key

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


def _cmp_fraction(a_num: int, a_den: int, b_num: int, b_den: int) -> int:
    """Sign of ``a_num/a_den - b_num/b_den`` using cross multiplication only."""
    lhs = a_num * b_den
    rhs = b_num * a_den
    return (lhs > rhs) - (lhs < rhs)


def trace_cells(
    vertices: list[tuple[int, int]],
    canvas_width: int,
    canvas_height: int,
    rows: int,
    cols: int,
) -> list[tuple[int, int]]:
    """Ordered, 1-based ``(row, col)`` cells entered by the crack polyline.

    ``vertices`` are already normalized and must lie inside the canvas
    (``0 <= u < canvas_width``, ``0 <= v < canvas_height``).

    The cell bands come from the same ownership as :func:`locate_cell`: strip
    ``k`` owns the half-open region ``[(k-1)*span/divisions, k*span/divisions)``,
    so the grid line between strips ``k`` and ``k+1`` sits at the fractional
    position ``k*span/divisions`` (in a non-divisible layout it cuts through a
    pixel). Every crossing parameter is kept as the integer fraction
    ``(k*span - p0*divisions) / (delta*divisions)`` and compared solely by
    cross multiplication -- no float rounding anywhere.

    Conventions:

    * cell regions are closed on the top/left, open on the bottom/right; the
      canvas's outer right/bottom edges are closed;
    * a segment running along a divider stays in the strip on its right / lower
      side (a constant coordinate never generates an event on that axis);
    * a right/down crossing whose endpoint lies on a divider enters the
      right/lower strip; a left/up segment ending on a divider keeps the
      point's (right/lower) ownership, so that crossing is excluded;
    * when a horizontal and a vertical divider are reached at the same
      parameter the segment enters the diagonal cell directly -- the two side
      cells, touched only at the corner, are not recorded.

    Consecutive duplicate cells are collapsed; a cell revisited after other
    cells in between is kept. Repeated vertices are zero-length segments and
    add no record.
    """
    def strip(value: int, divisions: int, span: int) -> int:
        return (value * divisions) // span + 1

    path: list[tuple[int, int]] = []

    def emit(row: int, col: int) -> None:
        if not path or path[-1] != (row, col):
            path.append((row, col))

    u0, v0 = vertices[0]
    row = strip(v0, rows, canvas_height)
    col = strip(u0, cols, canvas_width)
    emit(row, col)

    for u1, v1 in vertices[1:]:
        if (u1, v1) == (u0, v0):
            # Zero-length segment: no new cell can be entered.
            continue

        dx, dy = u1 - u0, v1 - v0
        # event: (parameter numerator, denominator, axis, divider index).
        # axis 0 crosses vertical divider k (columns k / k+1), axis 1 crosses
        # horizontal divider l (rows l / l+1).
        events: list[tuple[int, int, int, int]] = []
        if dx > 0:
            denominator = dx * cols
            for divider in range(1, cols):
                numerator = divider * canvas_width - u0 * cols
                if 0 < numerator <= denominator:
                    events.append((numerator, denominator, 0, divider))
        elif dx < 0:
            denominator = -dx * cols
            for divider in range(1, cols):
                numerator = u0 * cols - divider * canvas_width
                if 0 <= numerator < denominator:
                    events.append((numerator, denominator, 0, divider))
        if dy > 0:
            denominator = dy * rows
            for divider in range(1, rows):
                numerator = divider * canvas_height - v0 * rows
                if 0 < numerator <= denominator:
                    events.append((numerator, denominator, 1, divider))
        elif dy < 0:
            denominator = -dy * rows
            for divider in range(1, rows):
                numerator = v0 * rows - divider * canvas_height
                if 0 <= numerator < denominator:
                    events.append((numerator, denominator, 1, divider))

        events.sort(key=cmp_to_key(
            lambda a, b: _cmp_fraction(a[0], a[1], b[0], b[1])
        ))

        index = 0
        while index < len(events):
            num, den = events[index][0], events[index][1]
            # Consume every event sharing this exact parameter: a horizontal
            # and vertical divider crossed together form one diagonal step.
            group_end = index + 1
            while group_end < len(events):
                other = events[group_end]
                if _cmp_fraction(other[0], other[1], num, den) != 0:
                    break
                group_end += 1

            for event_index in range(index, group_end):
                _, _, event_axis, event_divider = events[event_index]
                if event_axis == 0:
                    col = event_divider + 1 if dx > 0 else event_divider
                else:
                    row = event_divider + 1 if dy > 0 else event_divider
            emit(row, col)
            index = group_end

        # Resync at the vertex: the endpoint pixel itself is owned by the
        # locate_cell rule (right/lower side of any coincident divider).
        row = strip(v1, rows, canvas_height)
        col = strip(u1, cols, canvas_width)
        emit(row, col)
        u0, v0 = u1, v1

    return path
