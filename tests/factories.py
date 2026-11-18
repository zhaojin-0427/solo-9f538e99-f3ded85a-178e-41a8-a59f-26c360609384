"""Helpers building OSM-style revision payloads."""

from __future__ import annotations


def node(nid: int, lon: float, lat: float) -> dict:
    return {"id": nid, "lat": lat, "lon": lon}


def way(wid: int, refs: list[int], role: str = "outer") -> dict:
    return {"id": wid, "role": role, "node_refs": refs}


def revision(rev: int, nodes: list[dict], ways: list[dict]) -> dict:
    return {"revision": rev, "nodes": nodes, "ways": ways}


# Unit square outer ring, node ids 1..4, closed via repeated first ref.
SQUARE_NODES = [
    node(1, 0.0, 0.0),
    node(2, 1.0, 0.0),
    node(3, 1.0, 1.0),
    node(4, 0.0, 1.0),
]
SQUARE_WAY = way(10, [1, 2, 3, 4, 1])

# Inner square strictly inside the unit square.
INNER_NODES = [
    node(5, 0.2, 0.2),
    node(6, 0.8, 0.2),
    node(7, 0.8, 0.8),
    node(8, 0.2, 0.8),
]
INNER_WAY = way(20, [5, 6, 7, 8, 5], role="inner")
