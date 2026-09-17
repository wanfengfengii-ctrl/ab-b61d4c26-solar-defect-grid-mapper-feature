"""Request/response schemas with strict, batch-wide validation.

Any illegal item rejects the whole batch with HTTP 422; out-of-bounds points
report their array index via a custom error whose ``ctx['index']`` is surfaced
by the exception handler in :mod:`app.main`.
"""

from __future__ import annotations

from typing import Annotated, List, Literal

from pydantic import BaseModel, Field, StrictInt, model_validator
from pydantic_core import PydanticCustomError

# Positive, strictly-typed integer: rejects floats, strings and booleans so
# that e.g. width=3.5 or "10" never passes validation silently.
PositiveInt = Annotated[StrictInt, Field(gt=0)]


class Point(BaseModel):
    """One dark-spot pixel in the raw camera image (origin top-left)."""

    x: StrictInt
    y: StrictInt


class InspectRequest(BaseModel):
    """Batch of dark spots on one EL image."""

    width: PositiveInt
    height: PositiveInt
    rows: PositiveInt
    cols: PositiveInt
    rotation: Literal[0, 90, 180, 270]
    points: List[Point] = Field(min_length=1)

    @model_validator(mode="after")
    def _points_within_image(self) -> "InspectRequest":
        """Enforce 0 <= x < width and 0 <= y < height for every point.

        The first offending point aborts the whole batch; its index travels in
        the error context so the 422 response can name the exact array element.
        """
        for index, point in enumerate(self.points):
            if not (0 <= point.x < self.width and 0 <= point.y < self.height):
                raise PydanticCustomError(
                    "point_out_of_bounds",
                    "points[{index}] = ({x}, {y}) violates 0 <= x < {width} and 0 <= y < {height}",
                    {
                        "index": index,
                        "x": point.x,
                        "y": point.y,
                        "width": self.width,
                        "height": self.height,
                    },
                )
        return self


class SpotResult(BaseModel):
    """Normalized coordinates and 1-based cell address of one dark spot."""

    index: int
    x: int
    y: int
    row: int
    col: int


class Canvas(BaseModel):
    """Canvas size after rotation normalization."""

    width: int
    height: int


class Grid(BaseModel):
    """Cell grid of the module."""

    rows: int
    cols: int


class InspectResponse(BaseModel):
    rotation: int
    canvas: Canvas
    grid: Grid
    results: List[SpotResult]
