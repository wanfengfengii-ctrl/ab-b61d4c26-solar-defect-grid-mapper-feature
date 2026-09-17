"""Pure integer geometry for EL image normalization and cell lookup.

The EL camera may be mounted in four orientations on the production line, so a
raw image can be rotated 0/90/180/270 degrees clockwise relative to the upright
module. All functions here operate on discrete pixel indices with exact integer
arithmetic; the origin is the top-left corner of the image.
"""

from __future__ import annotations

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
