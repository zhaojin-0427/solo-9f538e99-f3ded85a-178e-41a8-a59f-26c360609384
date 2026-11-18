"""Unit tests for the way-stitching / polygon-validation core."""

from __future__ import annotations

import pytest
from shapely.geometry import shape

from app.geometry import GeometryError, build_polygon_geojson
from app.models import RevisionIn

from .factories import (
    INNER_NODES,
    INNER_WAY,
    SQUARE_NODES,
    SQUARE_WAY,
    node,
    revision,
    way,
)


def build(payload: dict):
    return build_polygon_geojson(RevisionIn(**payload))


# --------------------------------------------------------------------- happy path
def test_single_closed_outer_way_produces_polygon():
    geom = build(revision(1, SQUARE_NODES, [SQUARE_WAY]))
    assert geom["type"] == "Polygon"
    poly = shape(geom)
    assert poly.is_valid
    assert poly.area == pytest.approx(1.0)
    # GeoJSON order: lon, lat and the ring closes on the first point.
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1] == (0.0, 0.0)
    assert len(geom["coordinates"]) == 1  # no inner ring


def test_unordered_ways_stitched_with_reversed_segments():
    # Split the square into 4 unordered edge ways; one is given reversed.
    ways = [
        way(11, [2, 3]),
        way(12, [1, 4]),  # reversed relative to ring direction (4 -> 1 expected)
        way(13, [4, 3]),  # reversed relative to ring direction (3 -> 4 expected)
        way(14, [1, 2]),
    ]
    geom = build(revision(1, SQUARE_NODES, ways))
    poly = shape(geom)
    assert poly.is_valid
    assert poly.area == pytest.approx(1.0)
    ring = geom["coordinates"][0]
    assert ring[0] == ring[-1]
    assert len(ring) == 5  # 4 corners + closing point


def test_polygon_with_inner_ring():
    payload = revision(2, SQUARE_NODES + INNER_NODES, [SQUARE_WAY, INNER_WAY])
    geom = build(payload)
    poly = shape(geom)
    assert poly.is_valid
    assert len(geom["coordinates"]) == 2
    # area is outer minus inner (inner square side 0.6 -> area 0.36)
    assert poly.area == pytest.approx(1.0 - 0.36)


# ------------------------------------------------------------------ error classes
def test_missing_node_is_rejected_with_affected_way():
    ways = [way(10, [1, 2, 3, 99, 1])]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, SQUARE_NODES, ways))
    assert exc.value.code == "missing_nodes"
    assert exc.value.way_ids == [10]
    assert "99" in exc.value.message


def test_broken_ring_gap_between_ways():
    # node 3 only appears once -> open chain
    ways = [way(11, [1, 2, 3]), way(12, [4, 1])]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, SQUARE_NODES, ways))
    assert exc.value.code == "broken_ring"
    assert set(exc.value.way_ids) == {11, 12}


def test_single_unclosed_way_is_broken_ring():
    with pytest.raises(GeometryError) as exc:
        build(revision(1, SQUARE_NODES, [way(10, [1, 2, 3, 4])]))
    assert exc.value.code == "broken_ring"
    assert exc.value.way_ids == [10]


def test_fork_three_ways_meet_at_one_node():
    # Three edge ends all meet at node 1.
    ways = [way(11, [1, 2]), way(12, [1, 3]), way(13, [1, 4]), way(14, [2, 3, 4, 2])]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, SQUARE_NODES, ways))
    assert exc.value.code == "fork"


def test_t_junction_way_ending_on_another_ways_interior():
    # Outer square split so that an extra way ends on an interior node.
    nodes = SQUARE_NODES + [node(9, 0.5, 0.0), node(10, 0.5, 0.5)]
    ways = [
        way(11, [1, 9, 2, 3, 4, 1]),       # node 9 interior to this way
        way(12, [9, 10]),                  # ends at node 9 -> T-junction
    ]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, nodes, ways))
    assert exc.value.code == "fork"
    assert set(exc.value.way_ids) == {11, 12}


def test_self_intersecting_figure_eight():
    # Bowtie: 1->2->3->4->1 with crossing diagonals via ordering
    # (0,0) (1,1) (0,1) (1,0) (0,0)
    nodes = [
        node(1, 0.0, 0.0),
        node(2, 1.0, 1.0),
        node(3, 0.0, 1.0),
        node(4, 1.0, 0.0),
    ]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, nodes, [way(10, [1, 2, 3, 4, 1])]))
    assert exc.value.code == "self_intersection"


def test_inner_ring_touching_outer_boundary_is_rejected():
    inner_nodes = [
        node(5, 0.0, 0.0),  # touches outer corner node 1
        node(6, 0.5, 0.0),
        node(7, 0.5, 0.5),
    ]
    ways = [SQUARE_WAY, way(20, [5, 6, 7, 5], role="inner")]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, SQUARE_NODES + inner_nodes, ways))
    assert exc.value.code == "inner_ring_violation"
    assert exc.value.way_ids == [20]


def test_inner_ring_crossing_outer_boundary_is_rejected():
    inner_nodes = [
        node(5, -0.5, 0.5),
        node(6, 0.5, 0.5),
        node(7, 0.5, 1.5),
        node(8, -0.5, 1.5),
    ]
    ways = [SQUARE_WAY, way(20, [5, 6, 7, 8, 5], role="inner")]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, SQUARE_NODES + inner_nodes, ways))
    assert exc.value.code == "inner_ring_violation"


def test_inner_ring_outside_outer_is_rejected():
    inner_nodes = [
        node(5, 5.0, 5.0),
        node(6, 6.0, 5.0),
        node(7, 6.0, 6.0),
        node(8, 5.0, 6.0),
    ]
    ways = [SQUARE_WAY, way(20, [5, 6, 7, 8, 5], role="inner")]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, SQUARE_NODES + inner_nodes, ways))
    assert exc.value.code == "inner_ring_violation"


def test_missing_outer_role():
    payload = revision(1, INNER_NODES, [INNER_WAY])
    with pytest.raises(GeometryError) as exc:
        build(payload)
    assert exc.value.code == "missing_outer"


def test_disconnected_outer_components():
    # Two separate closed ways both labelled outer, only 8 nodes allowed-ish.
    nodes = SQUARE_NODES + [
        node(5, 2.0, 2.0),
        node(6, 3.0, 2.0),
        node(7, 3.0, 3.0),
        node(8, 2.0, 3.0),
    ]
    ways = [way(10, [1, 2, 3, 4, 1]), way(20, [5, 6, 7, 8, 5])]
    with pytest.raises(GeometryError) as exc:
        build(revision(1, nodes, ways))
    assert exc.value.code == "broken_ring"
