"""Pydantic request/response models.

Each submission is a *complete* snapshot of one relation revision:
all nodes referenced by the ways must be included, and the ways are
unordered (the stitcher may traverse them in either direction).
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Hard limits required by the specification.
MAX_WAYS_PER_REVISION = 8
MAX_NODES_PER_REVISION = 32


class NodeIn(BaseModel):
    id: int = Field(..., ge=1, description="OSM node id")
    lat: float = Field(..., ge=-90.0, le=90.0, description="Latitude (WGS84)")
    lon: float = Field(..., ge=-180.0, le=180.0, description="Longitude (WGS84)")


class WayIn(BaseModel):
    id: int = Field(..., ge=1, description="OSM way id")
    role: Literal["outer", "inner"] = Field(..., description="Relation role")
    node_refs: list[int] = Field(
        ...,
        min_length=2,
        description="Ordered node ids as they appear in the OSM way; "
        "traversal may be reversed while stitching.",
    )


class RevisionIn(BaseModel):
    revision: int = Field(..., ge=1, description="Monotonically increasing revision number")
    nodes: list[NodeIn] = Field(..., min_length=1, max_length=MAX_NODES_PER_REVISION)
    ways: list[WayIn] = Field(..., min_length=1, max_length=MAX_WAYS_PER_REVISION)

    @model_validator(mode="after")
    def _unique_ids(self) -> "RevisionIn":
        node_ids = [n.id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node id in nodes")
        way_ids = [w.id for w in self.ways]
        if len(set(way_ids)) != len(way_ids):
            raise ValueError("duplicate way id in ways")
        return self


class ErrorDetail(BaseModel):
    code: str
    message: str
    way_ids: list[int] = Field(default_factory=list)


class RevisionResult(BaseModel):
    relation_id: int
    revision: int
    status: Literal["valid", "invalid"]
    geometry: dict | None = Field(
        default=None, description="GeoJSON Polygon, present only when status is valid"
    )
    error: ErrorDetail | None = None
    input: dict | None = Field(
        default=None,
        description="Retained raw submission of the stored revision "
        "(populated by the per-revision read endpoint)",
    )
    last_valid_revision: int | None = Field(
        default=None,
        description="Most recent valid revision still readable while the current one is invalid",
    )
