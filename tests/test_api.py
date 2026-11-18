"""End-to-end API tests: state machine, persistence, atomicity, concurrency."""

from __future__ import annotations

import os
import threading

from fastapi.testclient import TestClient

from app.database import Database
from app.main import app

from .factories import (
    INNER_NODES,
    INNER_WAY,
    SQUARE_NODES,
    SQUARE_WAY,
    node,
    revision,
    way,
)


def post(client: TestClient, relation_id: int, payload: dict):
    return client.post(f"/relations/{relation_id}/revisions", json=payload)


# --------------------------------------------------------------- upload & status
def test_valid_upload_returns_geojson_and_is_served(client: TestClient, relation_id: int):
    resp = post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "valid"
    assert body["geometry"]["type"] == "Polygon"
    assert body["error"] is None
    assert body["last_valid_revision"] == 1

    current = client.get(f"/relations/{relation_id}/current").json()
    assert current["revision"] == 1
    assert current["geometry"] is not None


def test_invalid_revision_is_retained_and_current_stops_serving(
    client: TestClient, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))

    # Revision 2 references a missing node -> invalid but stored.
    bad = revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])])
    resp = post(client, relation_id, bad)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["geometry"] is None
    assert body["error"]["code"] == "missing_nodes"
    assert body["error"]["way_ids"] == [10]
    assert body["last_valid_revision"] == 1

    # Current face stops being served ...
    cur = client.get(f"/relations/{relation_id}/current")
    assert cur.status_code == 409
    assert cur.json()["last_valid_revision"] == 1

    # ... while the previous valid face remains readable by revision number,
    # and the invalid revision retains its raw input.
    old = client.get(f"/relations/{relation_id}/revisions/1").json()
    assert old["status"] == "valid" and old["geometry"] is not None

    bad_read = client.get(f"/relations/{relation_id}/revisions/2")
    assert bad_read.status_code == 200
    stored = bad_read.json()
    assert stored["status"] == "invalid"
    assert stored["input"]["revision"] == 2
    assert stored["input"]["ways"][0]["node_refs"] == [1, 2, 3, 99, 1]
    assert stored["error"]["code"] == "missing_nodes"


def test_later_valid_revision_resumes_serving(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    assert client.get(f"/relations/{relation_id}/current").status_code == 409

    # Revision 3: valid polygon with an inner ring.
    good3 = revision(3, SQUARE_NODES + INNER_NODES, [SQUARE_WAY, INNER_WAY])
    resp = post(client, relation_id, good3)
    assert resp.json()["status"] == "valid"

    cur = client.get(f"/relations/{relation_id}/current")
    assert cur.status_code == 200
    assert cur.json()["revision"] == 3
    assert len(cur.json()["geometry"]["coordinates"]) == 2


def test_revision_numbers_must_strictly_increase(client: TestClient, relation_id: int):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    conflict = post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "revision_conflict"

    # A lower revision number is rejected as well.
    conflict2 = post(client, relation_id, revision(0, SQUARE_NODES, [SQUARE_WAY]))
    assert conflict2.status_code == 422  # pydantic rejects revision < 1

    # nothing extra was stored
    listing = client.get(f"/relations/{relation_id}/revisions").json()["revisions"]
    assert [r["revision"] for r in listing] == [1]


def test_not_found_paths(client: TestClient, relation_id: int):
    assert client.get(f"/relations/{relation_id}/current").status_code == 404
    assert client.get(f"/relations/{relation_id}/revisions/7").status_code == 404


def test_input_limits_enforced_by_validation(client: TestClient, relation_id: int):
    # 9 ways -> 422, nothing stored.
    too_many_ways = [way(100 + i, [1, 2]) for i in range(9)]
    resp = post(client, relation_id, revision(1, SQUARE_NODES, too_many_ways))
    assert resp.status_code == 422
    assert client.get(f"/relations/{relation_id}/revisions").json()["revisions"] == []

    # 33 nodes -> 422.
    many_nodes = [node(i, 0.0, 0.0) for i in range(1, 34)]
    one_way = way(1, [many_nodes[0]["id"], many_nodes[1]["id"], many_nodes[0]["id"]])
    resp = post(client, relation_id, {"revision": 1, "nodes": many_nodes, "ways": [one_way]})
    assert resp.status_code == 422

    # unknown role -> 422
    bad_role = {"revision": 1, "nodes": SQUARE_NODES,
                "ways": [{**SQUARE_WAY, "role": "boundary"}]}
    assert post(client, relation_id, bad_role).status_code == 422


# ------------------------------------------------------------------ durability
def test_data_survives_database_restart(
    client: TestClient, db_path: str, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post(client, relation_id, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))

    # Simulate process restart: brand new Database over the same file.
    restarted = Database(db_path)
    app.state.db = restarted

    row = restarted.get_revision(relation_id, 2)
    assert row is not None
    assert row["status"] == "invalid"
    assert row["input"]["ways"][0]["node_refs"] == [1, 2, 3, 99, 1]
    assert row["error"]["code"] == "missing_nodes"
    valid_row = restarted.get_revision(relation_id, 1)
    assert valid_row is not None and valid_row["geometry"]["type"] == "Polygon"
    assert restarted.get_last_valid(relation_id)["revision"] == 1


def test_wal_files_are_persistent(db_path: str):
    Database(db_path)
    assert os.path.exists(db_path)
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


# ------------------------------------------------------------------- atomicity
def test_failed_transaction_leaves_no_half_revision(db_path: str, relation_id: int):
    db = Database(db_path)
    good = revision(1, SQUARE_NODES, [SQUARE_WAY])
    db.save_revision(relation_id, 1, "valid", good, {"type": "Polygon"}, None)

    # Force the INSERT to fail mid-transaction with a CHECK violation.
    try:
        db.save_revision(relation_id, 2, "bogus-status", good, None, None)  # type: ignore[arg-type]
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected storage failure")

    # Only the fully committed revision exists; no half row for revision 2.
    assert db.get_revision(relation_id, 2) is None
    assert db.get_latest_revision(relation_id)["revision"] == 1
    assert db.list_revisions(relation_id) == [{"revision": 1, "status": "valid"}]


def test_unwritable_storage_keeps_empty_database(tmp_path, relation_id: int):
    bad_dir = tmp_path / "readonly"
    bad_dir.mkdir()
    os.chmod(bad_dir, 0o500)  # read+execute, no write
    try:
        try:
            Database(str(bad_dir / "flood.db"))
        except Exception:
            pass
        else:  # pragma: no cover - running as root would bypass perms
            import pytest as _pt

            _pt.skip("permissions not enforced for this user")
        # Directory contains no usable database artefacts.
        assert not (bad_dir / "flood.db").exists()
    finally:
        os.chmod(bad_dir, 0o700)


# ----------------------------------------------------------------- concurrency
def test_concurrent_uploads_serialize_and_never_corrupt_state(
    client: TestClient, relation_id: int
):
    errors: list[Exception] = []
    # Increasing revisions fired from many threads. BEGIN IMMEDIATE serialises
    # the transactions; revisions arriving out of order are rejected with 409,
    # so the guarantees under test are: no duplicates, stored revisions are
    # ordered, every stored row is complete and no error ever corrupts storage.
    revisions = list(range(10, 26))
    accepted: list[int] = []
    rejected: list[int] = []

    lock = threading.Lock()

    def upload(rev: int) -> None:
        try:
            r = post(client, relation_id, revision(rev, SQUARE_NODES, [SQUARE_WAY]))
            assert r.status_code in (200, 409), r.text
            with lock:
                (accepted if r.status_code == 200 else rejected).append(rev)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=upload, args=(rev,)) for rev in revisions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    listing = client.get(f"/relations/{relation_id}/revisions").json()["revisions"]
    stored = [r["revision"] for r in listing]
    assert len(stored) == len(accepted)
    assert stored == sorted(set(stored))  # no duplicates, stored ordered
    assert all(r["status"] == "valid" for r in listing)
    # Every requested revision was decided exactly once.
    assert sorted(accepted + rejected) == revisions
    assert set(rejected).isdisjoint(set(stored))

    # Readers always observe complete revisions with matching state.
    for rev in stored:
        row = client.get(f"/relations/{relation_id}/revisions/{rev}").json()
        assert row["status"] == "valid"
        assert row["geometry"]["type"] == "Polygon"
        assert row["input"]["revision"] == rev


def test_concurrent_read_during_write_never_sees_half_state(
    client: TestClient, db: Database, relation_id: int
):
    post(client, relation_id, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    stop = threading.Event()
    bad_snapshots: list[dict] = []

    # A separate TestClient has its own anyio portal: this mirrors two
    # real HTTP clients and avoids re-entering the same portal from the
    # test thread while the main thread is blocked inside a request.
    reader_client = TestClient(app)
    reader_client.__enter__()
    # Its lifespan swaps in a default database; point it back at the test
    # database so both clients hit identical storage.
    app.state.db = db

    def reader() -> None:
        while not stop.is_set():
            r = reader_client.get(f"/relations/{relation_id}/revisions")
            for item in r.json()["revisions"]:
                if item["status"] not in ("valid", "invalid"):
                    bad_snapshots.append(item)  # pragma: no cover

    t = threading.Thread(target=reader)
    t.start()
    try:
        # Monotonic revision numbers: every write is accepted and readers
        # only ever observe whole committed revisions.
        for rev in range(2, 12):
            r = post(client, relation_id, revision(rev, SQUARE_NODES, [SQUARE_WAY]))
            assert r.status_code == 200
        stop.set()
        t.join(timeout=10)
        assert not t.is_alive()
    finally:
        stop.set()
        reader_client.__exit__(None, None, None)
    assert bad_snapshots == []
    assert (
        len(client.get(f"/relations/{relation_id}/revisions").json()["revisions"]) == 11
    )


def test_duplicate_revision_rejected_without_corrupting_state(
    client: TestClient, relation_id: int
):
    # Deterministic duplicate-number conflict (the concurrent case relies on
    # thread scheduling, so semantics are asserted directly here).
    first = post(client, relation_id, revision(10, SQUARE_NODES, [SQUARE_WAY]))
    assert first.status_code == 200
    dup = post(client, relation_id, revision(10, SQUARE_NODES, [SQUARE_WAY]))
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "revision_conflict"
    listing = client.get(f"/relations/{relation_id}/revisions").json()["revisions"]
    assert [r["revision"] for r in listing] == [10]
    assert listing[0]["status"] == "valid"


def test_docs_page_available(client: TestClient):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/relations/{relation_id}/revisions" in schema["paths"]
    assert "/relations/{relation_id}/current" in schema["paths"]
