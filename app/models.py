"""Request/response schemas with strict, batch-wide validation.

Any illegal item rejects the whole batch with HTTP 422; out-of-bounds points
report their array index via a custom error whose ``ctx['index']`` is surfaced
by the exception handler in :mod:`app.main`.
"""

from __future__ import annotations

from typing import Any, Annotated, List, Literal

from pydantic import BaseModel, Field, StrictInt, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError, ValidationError

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


class Vertex(BaseModel):
    """One marked center of a dark spot along the crack (raw image pixel)."""

    x: StrictInt
    y: StrictInt


def _is_strict_int(value: Any) -> bool:
    """Accept ``int`` but reject ``bool`` (which is an ``int`` subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


class TraceRequest(BaseModel):
    """Crack polyline on one EL image: same canvas/grid/orientation fields as inspect."""

    width: PositiveInt
    height: PositiveInt
    rows: PositiveInt
    cols: PositiveInt
    rotation: Literal[0, 90, 180, 270]
    vertices: List[Vertex] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _vertices_valid_or_all_indices(cls, data: Any) -> Any:
        """Validate every vertex by raw-input position, collecting *all* errors.

        Pydantic normally stops a model validator once a list item fails its
        own type validation, which would hide out-of-bounds siblings of a
        malformed vertex.  Here the raw payload is checked manually and one
        error per offending vertex/coordinate is raised together; the request
        handler still applies the regular schema validation afterwards.
        """
        if not isinstance(data, dict):
            return data
        width, height = data.get("width"), data.get("height")
        vertices = data.get("vertices")
        if not (
            _is_strict_int(width)
            and width > 0
            and _is_strict_int(height)
            and height > 0
            and isinstance(vertices, list)
        ):
            return data

        errors: List[InitErrorDetails] = []
        for index, item in enumerate(vertices):
            if not isinstance(item, dict):
                errors.append(
                    InitErrorDetails(
                        type="model_type",
                        loc=("vertices", index),
                        input=item,
                        ctx={"class_name": "Vertex"},
                    )
                )
                continue
            for axis, bound in (("x", width), ("y", height)):
                if axis not in item:
                    errors.append(InitErrorDetails(type="missing", loc=("vertices", index, axis)))
                    continue
                value = item[axis]
                if not _is_strict_int(value):
                    errors.append(
                        InitErrorDetails(type="int_type", loc=("vertices", index, axis), input=value)
                    )
                    continue
                if not 0 <= value < bound:
                    errors.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "vertex_out_of_bounds",
                                "vertices[{index}] = ({x}, {y}) violates "
                                "0 <= x < {width} and 0 <= y < {height}",
                                {
                                    "index": index,
                                    "x": item.get("x"),
                                    "y": item.get("y"),
                                    "width": width,
                                    "height": height,
                                },
                            ),
                            loc=("vertices", index),
                            input={"x": item.get("x"), "y": item.get("y")},
                        )
                    )
        if errors:
            raise ValidationError.from_exception_data(cls.__name__, errors)
        return data


class PathPoint(BaseModel):
    """One ordered cell of the crack path (1-based row/column)."""

    row: int
    col: int


class NormalizedVertex(BaseModel):
    """Polyline vertex on the normalized (upright) canvas."""

    x: int
    y: int


class TraceResponse(BaseModel):
    rotation: int
    canvas: Canvas
    grid: Grid
    vertices: List[NormalizedVertex]
    path: List[PathPoint]
