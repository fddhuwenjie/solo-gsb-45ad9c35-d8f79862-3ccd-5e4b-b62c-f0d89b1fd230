import sqlite3
import threading

import pytest

import app.storage as storage_mod
from app.oci import OCI_IMAGE_MANIFEST
from app.storage import Repository
from conftest import child, index, ingest, manifest


def plat(arch="amd64"):
    return {"os": "linux", "architecture": arch}


def promote(client, name, root, platforms=None):
    return client.post(
        f"/channels/{name}/promote",
        json={"rootDigest": root, "platforms": platforms or [plat()]},
    )


def gc(client, payload="ABSENT"):
    if payload == "ABSENT":
        return client.post("/maintenance/gc")
    return client.post("/maintenance/gc", json=payload)


def build_promoted_index(client):
    """Index -> (amd64 manifest, arm64 manifest), promoted to 'stable'."""
    m_amd_d, m_amd_raw, _ = manifest("sha256:" + "a" * 64)
    m_arm_d, m_arm_raw, _ = manifest("sha256:" + "b" * 64)
    ingest(client, m_amd_raw)
    ingest(client, m_arm_raw)
    idx_d, idx_raw, _ = index([
        child(m_amd_d, "linux", "amd64"),
        child(m_arm_d, "linux", "arm64"),
    ])
    ingest(client, idx_raw)
    r = promote(client, "stable", idx_d, [plat(), plat("arm64")])
    assert r.status_code == 200, r.text
    return idx_d, m_amd_d, m_arm_d


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_reports_candidates_without_deleting(client):
    idx_d, m_amd_d, _ = build_promoted_index(client)

    orphan_m_d, orphan_m_raw, _ = manifest("sha256:" + "c" * 64)
    ingest(client, orphan_m_raw)
    # Orphan index pointing at a *reachable* manifest: the index itself is
    # garbage, the shared child is not.
    orphan_idx_d, orphan_idx_raw, _ = index([child(m_amd_d, "linux", "amd64")])
    ingest(client, orphan_idx_raw)

    r = gc(client, {"dryRun": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dryRun"] is True
    assert body["roots"] == 1
    assert body["reachable"] == 3
    assert body["candidates"] == 2
    assert body["digests"] == sorted([orphan_m_d, orphan_idx_d])
    assert body["bytes"] == len(orphan_m_raw) + len(orphan_idx_raw)
    assert body["remaining"] == 0

    # A dry run must not modify the store.
    assert client.get("/manifests").json()["count"] == 5
    assert client.get(f"/manifests/{orphan_m_d}").status_code == 200
    assert client.get(f"/manifests/{orphan_idx_d}").status_code == 200


def test_gc_on_empty_store(client):
    r = gc(client)
    assert r.status_code == 200
    assert r.json() == {
        "dryRun": False,
        "roots": 0,
        "reachable": 0,
        "candidates": 0,
        "limit": None,
        "digests": [],
        "bytes": 0,
        "remaining": 0,
    }


# ---------------------------------------------------------------------------
# Real sweeps
# ---------------------------------------------------------------------------


def test_sweep_deletes_garbage_and_channels_still_resolve(client):
    idx_d, m_amd_d, m_arm_d = build_promoted_index(client)
    orphan_d, orphan_raw, _ = manifest("sha256:" + "c" * 64)
    ingest(client, orphan_raw)

    # dryRun defaults to false; an empty body is accepted too.
    r = gc(client, {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dryRun"] is False
    assert body["digests"] == [orphan_d]
    assert body["bytes"] == len(orphan_raw)
    assert body["remaining"] == 0

    assert client.get(f"/manifests/{orphan_d}").status_code == 404
    assert client.get("/manifests").json()["count"] == 3

    # The channel still resolves for every promoted platform.
    r = client.post("/resolve", json={"rootDigest": idx_d, "platform": plat()})
    assert r.status_code == 200
    assert r.json()["digest"] == m_amd_d
    r = client.post(
        "/resolve", json={"rootDigest": idx_d, "platform": plat("arm64")}
    )
    assert r.status_code == 200
    assert r.json()["digest"] == m_arm_d

    # Channel pointer and promotion history are untouched by the sweep.
    current = client.get("/channels/stable").json()
    assert current["currentDigest"] == idx_d
    assert current["sequence"] == 1
    events = client.get("/channels/stable/events").json()
    assert [item["sequence"] for item in events["items"]] == [1]


def test_promotion_history_roots_keep_superseded_trees(client):
    idx_a_d, m_a_d, _ = build_promoted_index(client)  # stable -> A (rev 1)

    m_b_d, m_b_raw, _ = manifest("sha256:" + "d" * 64)
    ingest(client, m_b_raw)
    idx_b_d, idx_b_raw, _ = index([child(m_b_d, "linux", "amd64")])
    ingest(client, idx_b_raw)
    r = promote(client, "stable", idx_b_d)  # stable -> B (rev 2)
    assert r.status_code == 200

    # The channel now points at B, but A's tree is still rooted by the
    # ledger's rootDigest entries and must survive the sweep.
    r = gc(client, {"dryRun": True})
    assert r.json()["roots"] == 2
    assert r.json()["candidates"] == 0

    r = gc(client)
    assert r.json()["digests"] == []
    for digest in (idx_a_d, m_a_d, idx_b_d, m_b_d):
        assert client.get(f"/manifests/{digest}").status_code == 200


def test_nested_index_tree_is_fully_kept(client):
    leaf_d, leaf_raw, _ = manifest("sha256:" + "e" * 64)
    ingest(client, leaf_raw)
    inner_d, inner_raw, _ = index([child(leaf_d, "linux", "amd64")])
    ingest(client, inner_raw)
    outer_d, outer_raw, _ = index([child(
        inner_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, outer_raw)
    assert promote(client, "stable", outer_d).status_code == 200

    orphan_d, orphan_raw, _ = manifest("sha256:" + "f" * 64)
    ingest(client, orphan_raw)

    body = gc(client).json()
    assert body["reachable"] == 3
    assert body["digests"] == [orphan_d]

    r = client.post("/resolve", json={"rootDigest": outer_d, "platform": plat()})
    assert r.status_code == 200
    assert r.json()["chain"] == [outer_d, inner_d, leaf_d]


def test_without_channels_everything_is_garbage(client):
    m_d, m_raw, _ = manifest()
    ingest(client, m_raw)
    idx_d, idx_raw, _ = index([child(m_d, "linux", "amd64")])
    ingest(client, idx_raw)

    body = gc(client).json()
    assert body["roots"] == 0
    assert body["candidates"] == 2
    assert body["digests"] == sorted([m_d, idx_d])
    assert client.get("/manifests").json()["count"] == 0


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------


def test_limit_deletes_smallest_digests_first(client):
    digests = []
    for ch in ("a", "b", "c"):
        d, raw, _ = manifest("sha256:" + ch * 64)
        ingest(client, raw)
        digests.append(d)
    expected_order = sorted(digests)

    # Dry run honours the limit as well and changes nothing.
    body = gc(client, {"dryRun": True, "limit": 2}).json()
    assert body["candidates"] == 3
    assert body["limit"] == 2
    assert body["digests"] == expected_order[:2]
    assert body["remaining"] == 1
    assert client.get("/manifests").json()["count"] == 3

    body = gc(client, {"limit": 2}).json()
    assert body["digests"] == expected_order[:2]
    assert body["remaining"] == 1
    assert client.get("/manifests").json()["count"] == 1
    assert client.get(f"/manifests/{expected_order[2]}").status_code == 200

    # A limit larger than the candidate count deletes everything left.
    body = gc(client, {"limit": 10}).json()
    assert body["digests"] == expected_order[2:]
    assert body["remaining"] == 0
    assert client.get("/manifests").json()["count"] == 0


# ---------------------------------------------------------------------------
# Validation and failure handling
# ---------------------------------------------------------------------------


def test_gc_validates_payload(client):
    for bad in ("yes", 1, 0, ["true"]):
        r = gc(client, {"dryRun": bad})
        assert r.status_code == 400, bad
        assert r.json()["error"]["code"] == "INVALID_REQUEST"

    for bad in (0, -3, 1.5, "2", True):
        r = gc(client, {"limit": bad})
        assert r.status_code == 400, bad
        assert r.json()["error"]["code"] == "INVALID_REQUEST"

    r = client.post("/maintenance/gc", json=[1, 2])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_gc_failure_maps_to_500(client, monkeypatch):
    def boom(_self, **_kwargs):
        raise storage_mod.GCError("boom")

    monkeypatch.setattr(Repository, "collect_garbage", boom)
    r = gc(client)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "GC_FAILED"


# ---------------------------------------------------------------------------
# Repository-level transaction semantics
# ---------------------------------------------------------------------------


def test_gc_transaction_blocks_concurrent_promotion(tmp_path, monkeypatch):
    # Shorten the busy timeout so the blocked promotion fails fast instead
    # of waiting the default five seconds.
    monkeypatch.setattr(storage_mod, "_BUSY_TIMEOUT_SECONDS", 0.1)
    repo = Repository(str(tmp_path / "gc-block.db"))

    digest = "sha256:" + "ab" * 32
    doc = {
        "schemaVersion": 2,
        "mediaType": OCI_IMAGE_MANIFEST,
        "config": {"digest": "sha256:" + "0" * 64},
    }
    repo.upsert(digest, OCI_IMAGE_MANIFEST, doc, 100)

    inside_gc = threading.Event()
    finish_gc = threading.Event()
    real_reachable = storage_mod.reachable_digests

    def blocking_reachable(documents, roots):
        inside_gc.set()
        assert finish_gc.wait(5)
        return real_reachable(documents, roots)

    monkeypatch.setattr(storage_mod, "reachable_digests", blocking_reachable)

    outcome = {}

    def run_gc():
        outcome["gc"] = repo.collect_garbage()

    thread = threading.Thread(target=run_gc)
    thread.start()
    assert inside_gc.wait(5)

    # The sweep holds the write lock: a promotion cannot start its own
    # compare-and-swap transaction and fails instead of interleaving.
    with pytest.raises(sqlite3.OperationalError):
        repo.promote_channel("stable", digest, digest, [])

    finish_gc.set()
    thread.join(5)
    assert outcome["gc"].digests == [digest]
    assert repo.count() == 0


def test_gc_rolls_back_when_a_stored_document_is_corrupt(tmp_path):
    db_path = str(tmp_path / "gc-corrupt.db")
    repo = Repository(db_path)

    good = "sha256:" + "cd" * 32
    repo.upsert(
        good,
        OCI_IMAGE_MANIFEST,
        {"schemaVersion": 2, "mediaType": OCI_IMAGE_MANIFEST, "config": {}},
        50,
    )
    # A corrupt row that bypasses ingest validation.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO manifests (digest, media_type, doc, bytes) "
        "VALUES (?, ?, ?, ?)",
        ("sha256:" + "ef" * 32, OCI_IMAGE_MANIFEST, "{not json", 9),
    )
    conn.commit()
    conn.close()

    with pytest.raises(storage_mod.GCError):
        repo.collect_garbage()

    # The failure rolled the transaction back: nothing was deleted, not
    # even the (unreferenced) healthy row.
    assert repo.count() == 2
    assert repo.get(good) is not None
