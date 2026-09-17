"""FastAPI application: normalize rotated EL images and locate dark-spot cells."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import Canvas, Grid, InspectRequest, InspectResponse, SpotResult
from .transform import locate_cell, normalize_point, normalized_canvas

app = FastAPI(
    title="EL Cell Locator",
    summary="Map dark spots on rotation-normalized EL images to PV module cells.",
    version="1.0.0",
)


def _point_index(error: Dict[str, Any]) -> Optional[int]:
    """Best-effort extraction of the offending ``points`` array index."""
    ctx = error.get("ctx") or {}
    if isinstance(ctx.get("index"), int):
        return ctx["index"]
    loc = list(error.get("loc") or [])
    if "points" in loc:
        pos = loc.index("points")
        if pos + 1 < len(loc) and isinstance(loc[pos + 1], int):
            return loc[pos + 1]
    return None


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 payload that names the array index of every offending point.

    The batch is rejected as a whole: the body only contains error details and
    never any partial computation results.
    """
    detail: List[Dict[str, Any]] = []
    for error in exc.errors():
        item: Dict[str, Any] = {
            "type": error.get("type"),
            "loc": list(error.get("loc") or []),
            "msg": error.get("msg"),
        }
        index = _point_index(error)
        if index is not None:
            item["index"] = index
        detail.append(item)
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/inspect", response_model=InspectResponse)
def inspect(payload: InspectRequest) -> InspectResponse:
    """Normalize every dark spot and compute its 1-based (row, col) cell.

    Results preserve the input order of ``points``; ``x``/``y`` in each result
    are the coordinates on the normalized (upright) canvas.
    """
    canvas_width, canvas_height = normalized_canvas(payload.width, payload.height, payload.rotation)
    results: List[SpotResult] = []
    for index, point in enumerate(payload.points):
        u, v = normalize_point(point.x, point.y, payload.width, payload.height, payload.rotation)
        row, col = locate_cell(u, v, canvas_width, canvas_height, payload.rows, payload.cols)
        results.append(SpotResult(index=index, x=u, y=v, row=row, col=col))
    return InspectResponse(
        rotation=payload.rotation,
        canvas=Canvas(width=canvas_width, height=canvas_height),
        grid=Grid(rows=payload.rows, cols=payload.cols),
        results=results,
    )
