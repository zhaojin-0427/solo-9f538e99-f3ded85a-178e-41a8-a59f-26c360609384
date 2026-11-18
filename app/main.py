"""FastAPI application: upload relation revisions and read water polygons."""

from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .database import Database, RevisionConflict
from .geometry import GeometryError, build_polygon_geojson
from .models import RevisionIn, RevisionResult

def _database_path() -> str:
    # Read lazily so tests (and redeployed env) can override FLOOD_DB_PATH.
    return os.environ.get("FLOOD_DB_PATH", "/data/flood.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(_database_path())
    yield


app = FastAPI(
    title="OSM Flood Water-Area API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Submit complete, incrementing revisions of an OSM water relation "
        "(nodes + unordered outer/inner ways). Valid revisions become a "
        "GeoJSON Polygon; invalid revisions retain their raw input and an "
        "error, while the last valid polygon keeps being served."
    ),
)


def _db(request: Request) -> Database:
    return request.app.state.db


def _stored_to_result(row: dict) -> RevisionResult:
    return RevisionResult(
        relation_id=row["relation_id"],
        revision=row["revision"],
        status=row["status"],
        geometry=row["geometry"],
        error=row["error"],
    )


@app.get("/health", tags=["meta"])
def health(request: Request) -> dict:
    db = _db(request)
    return {"status": "ok", "version": __version__, "db": db.path}


@app.post(
    "/relations/{relation_id}/revisions",
    response_model=RevisionResult,
    status_code=200,
    tags=["revisions"],
    summary="Upload one complete relation revision",
)
def upload_revision(relation_id: int, payload: RevisionIn, request: Request) -> RevisionResult:
    """Validate and store a revision.

    The raw input is always stored atomically together with its computed
    geometry/state. Geometric failure yields ``status="invalid"`` (HTTP
    200, the revision is retained); only a non-incrementing revision
    number is rejected (HTTP 409, nothing is stored).
    """
    error = None
    geometry = None
    status = "valid"
    try:
        geometry = build_polygon_geojson(payload)
    except GeometryError as exc:
        status = "invalid"
        error = exc.to_detail()

    db = _db(request)
    try:
        db.save_revision(
            relation_id=relation_id,
            revision=payload.revision,
            status=status,
            input_payload=payload.model_dump(),
            geometry=geometry,
            error=error,
        )
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "latest_revision": exc.latest,
            },
        ) from exc

    last_valid = db.get_last_valid(relation_id)
    return RevisionResult(
        relation_id=relation_id,
        revision=payload.revision,
        status=status,
        geometry=geometry,
        error=error,
        last_valid_revision=last_valid["revision"] if last_valid else None,
    )


@app.get(
    "/relations/{relation_id}/revisions/{revision}",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read input, geometry and status of one stored revision",
)
def read_revision(relation_id: int, revision: int, request: Request) -> RevisionResult:
    row = _db(request).get_revision(relation_id, revision)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "revision does not exist"},
        )
    result = _stored_to_result(row)
    result.input = row["input"]
    last_valid_row = _db(request).get_last_valid(relation_id)
    result.last_valid_revision = last_valid_row["revision"] if last_valid_row else None
    return result


@app.get(
    "/relations/{relation_id}/revisions",
    tags=["revisions"],
    summary="List all stored revisions of a relation",
)
def list_revisions(relation_id: int, request: Request) -> dict:
    return {"relation_id": relation_id, "revisions": _db(request).list_revisions(relation_id)}


@app.get(
    "/relations/{relation_id}/current",
    response_model=RevisionResult,
    tags=["revisions"],
    summary="Read the currently served (latest valid) polygon",
)
def read_current(relation_id: int, request: Request) -> RevisionResult:
    """Current face semantics.

    * latest revision valid  -> serve its polygon;
    * latest revision invalid -> stop serving: 409, but the response
      points at ``last_valid_revision`` which remains fully readable via
      the per-revision endpoint;
    * a later valid revision resumes serving automatically.
    """
    db = _db(request)
    latest = db.get_latest_revision(relation_id)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "no revision stored for this relation"},
        )
    last_valid = db.get_last_valid(relation_id)
    if latest["status"] == "valid":
        result = _stored_to_result(latest)
        result.last_valid_revision = last_valid["revision"] if last_valid else None
        return result

    return JSONResponse(
        status_code=409,
        content={
            "relation_id": relation_id,
            "revision": latest["revision"],
            "status": "invalid",
            "geometry": None,
            "error": latest["error"],
            "last_valid_revision": last_valid["revision"] if last_valid else None,
            "detail": (
                "current revision is invalid and the face is not being served; "
                "fetch last_valid_revision for the previous valid polygon"
            ),
        },
    )
