"""Request/response schemas with strict, batch-wide validation.

Any illegal item rejects the whole batch with HTTP 422; out-of-bounds points or
vertices report their array index via a custom error whose ``ctx['index']`` is
surfaced by the exception handler in :mod:`app.main`.

When several items are illegal at once, *every* offending index is returned
(input order); type-illegal coordinates and out-of-bounds coordinates are
collected together rather than failing on the first bad item.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, List, Literal

from pydantic import BaseModel, Field, StrictInt, model_validator
from pydantic_core import InitErrorDetails, PydanticCustomError, ValidationError

# Positive, strictly-typed integer: rejects floats, strings and booleans so
# that e.g. width=3.5 or "10" never passes validation silently.
PositiveInt = Annotated[StrictInt, Field(gt=0)]


class Point(BaseModel):
    """One marked pixel in the raw camera image (origin top-left)."""

    x: StrictInt
    y: StrictInt


def _is_strict_int(value: object) -> bool:
    """Integer but not a bool (``True``/``False`` are not pixel coordinates)."""
    return isinstance(value, int) and not isinstance(value, bool)


class _BoundedPointBatch(BaseModel):
    """Shared canvas/grid fields plus whole-batch point validation.

    Subclasses pick the request field name (``points`` or ``vertices``) via
    :attr:`_FIELD`.  The ``before`` model validator inspects the raw payload so
    that all illegal items can be reported in one pass even when their
    coordinates fail integer parsing.
    """

    width: PositiveInt
    height: PositiveInt
    rows: PositiveInt
    cols: PositiveInt
    rotation: Literal[0, 90, 180, 270]

    _FIELD: ClassVar[str] = "points"
    _SINGULAR: ClassVar[str] = "point"

    @model_validator(mode="before")
    @classmethod
    def _validate_bounded_batch(cls, data: object) -> object:
        if not isinstance(data, dict):
            # Let pydantic emit the standard body/structure error.
            return data

        field = cls._FIELD
        singular = cls._SINGULAR
        width = data.get("width")
        height = data.get("height")
        items = data.get(field)

        # If the scalar canvas dimensions are themselves invalid, defer to the
        # standard field validators (which already produce 422s).
        if not (_is_strict_int(width) and width > 0 and _is_strict_int(height) and height > 0):
            return data
        if not isinstance(items, list) or len(items) == 0:
            # Missing/wrong-type/empty list errors come from field validation.
            return data

        details: List[InitErrorDetails] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                details.append(
                    InitErrorDetails(
                        type=PydanticCustomError(
                            f"{singular}_invalid",
                            "{field}[{index}] must be an object with integer x and y",
                            {"field": field, "index": index},
                        ),
                        loc=(field, index),
                        input=item,
                    )
                )
                continue

            bad_coordinate = False
            for coord in ("x", "y"):
                if coord not in item or not _is_strict_int(item.get(coord)):
                    details.append(
                        InitErrorDetails(
                            type=PydanticCustomError(
                                f"{singular}_coordinate_invalid",
                                "{field}[{index}].{coord} must be an integer",
                                {"field": field, "index": index, "coord": coord},
                            ),
                            loc=(field, index, coord),
                            input=item.get(coord),
                        )
                    )
                    bad_coordinate = True
            if bad_coordinate:
                continue

            x, y = item["x"], item["y"]
            if not (0 <= x < width and 0 <= y < height):
                details.append(
                    InitErrorDetails(
                        type=PydanticCustomError(
                            f"{singular}_out_of_bounds",
                            "{field}[{index}] = ({x}, {y}) violates "
                            "0 <= x < {width} and 0 <= y < {height}",
                            {
                                "field": field,
                                "index": index,
                                "x": x,
                                "y": y,
                                "width": width,
                                "height": height,
                            },
                        ),
                        loc=(field, index),
                        input=item,
                    )
                )

        if details:
            raise ValidationError.from_exception_data(cls.__name__, details)
        return data


class InspectRequest(_BoundedPointBatch):
    """Batch of dark spots on one EL image."""

    points: List[Point] = Field(min_length=1)

    _FIELD: ClassVar[str] = "points"
    _SINGULAR: ClassVar[str] = "point"


class TraceRequest(_BoundedPointBatch):
    """Non-empty polyline of crack vertices on one EL image."""

    vertices: List[Point] = Field(min_length=1)

    _FIELD: ClassVar[str] = "vertices"
    _SINGULAR: ClassVar[str] = "vertex"


class SpotResult(BaseModel):
    """Normalized coordinates and 1-based cell address of one dark spot."""

    index: int
    x: int
    y: int
    row: int
    col: int


class Vertex(BaseModel):
    """Normalized polyline vertex on the upright canvas."""

    index: int
    x: int
    y: int


class CellRef(BaseModel):
    """One 1-based cell address on an ordered crack path."""

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


class TraceResponse(BaseModel):
    rotation: int
    canvas: Canvas
    grid: Grid
    vertices: List[Vertex]
    path: List[CellRef]
