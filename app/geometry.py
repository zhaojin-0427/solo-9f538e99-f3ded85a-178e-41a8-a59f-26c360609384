"""Stitch unordered OSM ways into a validated GeoJSON Polygon.

Rules implemented (per the flood-analysis contract):

* ways are unordered; each way segment may be traversed forwards or
  backwards while stitching;
* the *outer* role ways must form exactly one closed ring;
* the *inner* role ways, if present, must form exactly one closed ring;
* failures (missing nodes, broken rings, forks, self-intersection, an
  inner ring touching or crossing the outer ring) raise
  :class:`GeometryError` carrying the affected way ids so the caller can
  retain the raw revision and report the error.
"""

from __future__ import annotations

from collections import defaultdict

from shapely.geometry import LinearRing, Polygon, mapping
from shapely.validation import explain_validity

from .models import RevisionIn


class GeometryError(Exception):
    """Topological or geometric rejection of one revision."""

    def __init__(self, code: str, message: str, way_ids: list[int] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # de-duplicated, deterministic order
        self.way_ids = sorted(set(way_ids or []))

    def to_detail(self) -> dict:
        return {"code": self.code, "message": self.message, "way_ids": self.way_ids}


def _assemble_ring(role: str, ways: list, nodes: dict[int, tuple[float, float]]) -> list[int]:
    """Stitch ways of one role into a closed, ordered list of node ids.

    ``ways`` are all WayIn objects sharing the same role.  The returned
    list keeps the closing node equal to the first node (OSM/GeoJSON
    ring convention).
    """
    way_by_id = {w.id: w for w in ways}

    # endpoint id -> list of (way id, position 0=start / -1=end)
    endpoints: dict[int, list[tuple[int, int]]] = defaultdict(list)
    interior: dict[int, list[int]] = defaultdict(list)  # node inside a way, not an endpoint
    refs_of: dict[int, list[int]] = {}

    for way in ways:
        refs = way.node_refs
        refs_of[way.id] = refs
        # A closed way legitimately repeats its first node at the end;
        # any other repeated reference is a loop / self-touch.
        body = refs[:-1] if refs[0] == refs[-1] else refs
        if len(set(body)) != len(body):
            raise GeometryError(
                "self_intersection",
                f"way {way.id} repeats a node and cannot be part of a simple ring",
                [way.id],
            )
        endpoints[refs[0]].append((way.id, 0))
        endpoints[refs[-1]].append((way.id, -1))
        for ref in refs[1:-1]:
            interior[ref].append(way.id)

    # Fork: more than two segment ends meet at the same node.
    for node_id, incidences in endpoints.items():
        if len(incidences) > 2:
            raise GeometryError(
                "fork",
                f"node {node_id} joins {len(incidences)} way ends; a single ring requires exactly two",
                [wid for wid, _ in incidences],
            )

    # T-junction: a node is interior to one way but another way ends on it.
    t_ways: set[int] = set()
    for node_id, incidences in endpoints.items():
        if node_id in interior:
            t_ways.update(wid for wid, _ in incidences)
            t_ways.update(interior[node_id])
    if t_ways:
        raise GeometryError(
            "fork",
            "a way ends on the interior of another way (T-junction), which forks the ring",
            sorted(t_ways),
        )

    # A single way is expected to be explicitly closed.
    if len(ways) == 1:
        only = ways[0]
        if only.node_refs[0] != only.node_refs[-1]:
            raise GeometryError(
                "broken_ring",
                f"outer/inner role '{role}' consists of one way {only.id} that is not closed",
                [only.id],
            )
        return list(only.node_refs)

    # Multiple ways: every endpoint must be shared by exactly two ways.
    bad_nodes = [node_id for node_id, inc in endpoints.items() if len(inc) != 2]
    if bad_nodes:
        affected_ways = {
            wid for node_id in bad_nodes for wid, _ in endpoints[node_id]
        }
        raise GeometryError(
            "broken_ring",
            f"node(s) {sorted(bad_nodes)} are not shared by exactly two ways; "
            "the ways do not form a closed ring",
            sorted(affected_ways),
        )

    # Walk the chain, reversing segment orientations as needed.
    first_way = ways[0]
    ring_refs: list[int] = list(first_way.node_refs)
    used: set[int] = {first_way.id}
    while ring_refs[0] != ring_refs[-1]:
        current_end = ring_refs[-1]
        candidates = [wid for wid, _ in endpoints[current_end] if wid not in used]
        if not candidates:
            raise GeometryError(
                "broken_ring",
                f"ring cannot be closed at node {current_end}; no remaining way continues",
                sorted(used),
            )
        next_id = candidates[0]
        next_refs = refs_of[next_id]
        if next_refs[0] == current_end:
            addition = next_refs[1:]
        elif next_refs[-1] == current_end:
            addition = list(reversed(next_refs[:-1]))
        else:  # pragma: no cover - endpoints map guarantees this cannot happen
            raise GeometryError("broken_ring", "internal stitching error", [next_id])
        ring_refs.extend(addition)
        used.add(next_id)
        if len(used) > len(ways):  # pragma: no cover - defensive guard
            raise GeometryError("fork", "stitching loop did not terminate", sorted(used))

    if used != set(way_by_id):
        missing = sorted(set(way_by_id) - used)
        raise GeometryError(
            "broken_ring",
            f"role '{role}' splits into separate components; ways {missing} are disconnected",
            missing,
        )

    # Ring must not visit any node twice before closing (figure-eight etc.).
    if len(set(ring_refs[:-1])) != len(ring_refs) - 1:
        raise GeometryError(
            "self_intersection",
            f"stitched {role} ring visits a node more than once",
            sorted(used),
        )

    return ring_refs


def build_polygon_geojson(payload: RevisionIn) -> dict:
    """Validate one revision and return its GeoJSON Polygon geometry.

    Raises :class:`GeometryError` on any topology / geometry violation.
    """
    nodes = {n.id: (n.lon, n.lat) for n in payload.nodes}

    outer_ways = [w for w in payload.ways if w.role == "outer"]
    inner_ways = [w for w in payload.ways if w.role == "inner"]

    if not outer_ways:
        raise GeometryError(
            "missing_outer", "no way with role 'outer' is present", [w.id for w in payload.ways]
        )

    # Every referenced node must be supplied.
    missing_refs: set[int] = set()
    affected: set[int] = set()
    for way in payload.ways:
        for ref in way.node_refs:
            if ref not in nodes:
                missing_refs.add(ref)
                affected.add(way.id)
    if missing_refs:
        raise GeometryError(
            "missing_nodes",
            f"referenced node id(s) absent from nodes: {sorted(missing_refs)}",
            sorted(affected),
        )

    outer_refs = _assemble_ring("outer", outer_ways, nodes)

    inner_refs_list: list[list[int]] = []
    if inner_ways:
        # Contract: at most one inner ring -> all inner ways must belong to it.
        inner_refs_list = [_assemble_ring("inner", inner_ways, nodes)]

    def to_coords(refs: list[int]) -> list[tuple[float, float]]:
        return [nodes[ref] for ref in refs]

    outer_coords = to_coords(outer_refs)
    try:
        outer_ring = LinearRing(outer_coords)
    except Exception as exc:  # pragma: no cover - shapely raises ValueError subclasses
        raise GeometryError("self_intersection", f"outer ring is not a simple ring: {exc}",
                            [w.id for w in outer_ways]) from exc
    if not outer_ring.is_simple:
        raise GeometryError(
            "self_intersection",
            "outer ring self-intersects: " + explain_validity(Polygon(outer_ring)),
            [w.id for w in outer_ways],
        )

    inner_rings: list[LinearRing] = []
    for inner_refs in inner_refs_list:
        inner_coords = to_coords(inner_refs)
        try:
            inner_ring = LinearRing(inner_coords)
        except Exception as exc:  # pragma: no cover
            raise GeometryError("self_intersection", f"inner ring is not a simple ring: {exc}",
                                [w.id for w in inner_ways]) from exc
        if not inner_ring.is_simple:
            raise GeometryError(
                "self_intersection",
                "inner ring self-intersects: " + explain_validity(Polygon(inner_ring)),
                [w.id for w in inner_ways],
            )
        # Strict containment: reject touching (boundary contact),
        # crossing and lying outside. Polygon.contains alone ignores
        # boundary contact, so test the ring boundaries explicitly.
        outer_polygon = Polygon(outer_ring)
        inner_polygon = Polygon(inner_ring)
        touches = outer_polygon.boundary.intersects(inner_polygon.boundary)
        if touches or not outer_polygon.contains(inner_polygon):
            reason = explain_validity(Polygon(outer_ring, [inner_ring.coords[:]]))
            raise GeometryError(
                "inner_ring_violation",
                "inner ring touches, crosses or lies outside the outer ring"
                + (f": {reason}" if reason else ""),
                [w.id for w in inner_ways],
            )
        inner_rings.append(inner_ring)

    polygon = Polygon(outer_ring, [r.coords[:] for r in inner_rings])
    if not polygon.is_valid:
        raise GeometryError(
            "self_intersection",
            "resulting polygon is not geometrically valid: " + explain_validity(polygon),
            [w.id for w in payload.ways],
        )

    return mapping(polygon)
