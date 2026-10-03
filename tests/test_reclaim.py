"""Tests for manifest storage reclamation (POST /maintenance/reclaim)."""

import json
import threading

from conftest import child, index, ingest, manifest


def promote(client, name="stable", root=None, platforms=None, **extra):
    body = {
        "rootDigest": root,
        "platforms": platforms or [{"os": "linux", "architecture": "amd64"}],
        **extra,
    }
    return client.post(f"/channels/{name}/promote", json=body)


def reclaim(client, **body):
    return client.post("/maintenance/reclaim", json=body if body else None)


def digests_in(client):
    return {d["digest"] for d in client.get("/manifests").json()["documents"]}


def build_referenced_tree(client):
    """A promoted index over two manifests plus two unrelated orphans."""
    m_amd_d, m_amd_raw, _ = manifest("sha256:" + "a" * 64)
    m_arm_d, m_arm_raw, _ = manifest("sha256:" + "b" * 64)
    junk_d, junk_raw, _ = manifest("sha256:" + "c" * 64)
    other_d, other_raw, _ = manifest("sha256:" + "d" * 64)
    for raw in (m_amd_raw, m_arm_raw, junk_raw, other_raw):
        ingest(client, raw)
    idx_d, idx_raw, _ = index([
        child(m_amd_d, "linux", "amd64"),
        child(m_arm_d, "linux", "arm64"),
    ])
    ingest(client, idx_raw)
    promote(client, root=idx_d,
            platforms=[{"os": "linux", "architecture": "amd64"},
                       {"os": "linux", "architecture": "arm64"}])
    return {
        "index": idx_d,
        "amd": m_amd_d,
        "arm": m_arm_d,
        "junk": junk_d,
        "other": other_d,
    }


# ---------------------------------------------------------------------------
# Reachable set: current pointers, history roots and nested index subtrees
# ---------------------------------------------------------------------------


def test_deletes_only_unreferenced_documents(client):
    d = build_referenced_tree(client)

    r = reclaim(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dryRun"] is False
    assert {item["digest"] for item in body["deleted"]} == {
        d["junk"], d["other"]
    }
    assert body["deletedCount"] == 2
    assert body["deletedBytes"] == sum(
        item["bytes"] for item in body["deleted"]
    )
    assert body["remaining"] == 0
    assert body["reachable"] == 3  # index + two manifests
    assert body["roots"] == 1

    remaining = digests_in(client)
    assert remaining == {d["index"], d["amd"], d["arm"]}

    # Every channel must still resolve exactly as before reclamation.
    for arch, expected in (("amd64", d["amd"]), ("arm64", d["arm"])):
        resolved = client.post("/resolve", json={
            "rootDigest": d["index"],
            "platform": {"os": "linux", "architecture": arch},
        })
        assert resolved.status_code == 200
        assert resolved.json()["digest"] == expected


def test_nested_index_subtree_is_reachable(client):
    leaf_d, leaf_raw, _ = manifest("sha256:" + "8" * 64)
    ingest(client, leaf_raw)
    inner_d, inner_raw, _ = index([child(leaf_d, "linux", "amd64")])
    ingest(client, inner_raw)
    outer_d, outer_raw, _ = index([child(
        inner_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, outer_raw)
    junk_d, junk_raw, _ = manifest("sha256:" + "7" * 64)
    ingest(client, junk_raw)

    promote(client, root=outer_d)

    body = reclaim(client).json()
    assert {item["digest"] for item in body["deleted"]} == {junk_d}
    assert digests_in(client) == {outer_d, inner_d, leaf_d}
    assert body["reachable"] == 3


def test_promotion_history_roots_are_kept_after_pointer_moves(client):
    d = build_referenced_tree(client)

    # A new generation supersedes the old index; the old root disappears from
    # the current pointer but must survive in the promotion history.
    new_m_d, new_m_raw, _ = manifest("sha256:" + "e" * 64)
    ingest(client, new_m_raw)
    new_idx_d, new_idx_raw, _ = index(
        [child(new_m_d, "linux", "amd64")])
    ingest(client, new_idx_raw)
    promote(client, root=new_idx_d)

    body = reclaim(client).json()
    assert {item["digest"] for item in body["deleted"]} == {
        d["junk"], d["other"]
    }
    assert digests_in(client) == {
        d["index"], d["amd"], d["arm"], new_idx_d, new_m_d,
    }


def test_root_digests_from_other_channels_are_roots(client):
    d = build_referenced_tree(client)
    isolated_d, isolated_raw, _ = manifest("sha256:" + "5" * 64)
    ingest(client, isolated_raw)
    promote(client, name="beta", root=isolated_d)

    body = reclaim(client).json()
    assert {item["digest"] for item in body["deleted"]} == {
        d["junk"], d["other"]
    }
    assert body["roots"] == 2
    assert isolated_d in digests_in(client)


def test_without_any_roots_everything_is_reclaimable(client):
    d1, raw1, _ = manifest()
    d2, raw2, _ = manifest("sha256:" + "2" * 64)
    ingest(client, raw1)
    ingest(client, raw2)

    body = reclaim(client).json()
    assert body["roots"] == 0
    assert body["reachable"] == 0
    assert body["deletedCount"] == 2
    assert client.get("/manifests").json()["count"] == 0


# ---------------------------------------------------------------------------
# dryRun
# ---------------------------------------------------------------------------


def test_dry_run_reports_candidates_without_deleting(client):
    d = build_referenced_tree(client)

    r = reclaim(client, dryRun=True)
    assert r.status_code == 200
    body = r.json()
    assert body["dryRun"] is True
    assert {item["digest"] for item in body["deleted"]} == {
        d["junk"], d["other"]
    }
    assert body["deletedBytes"] > 0
    # Candidates still exist; a subsequent real run reclaims the same set.
    assert digests_in(client) == {
        d["index"], d["amd"], d["arm"], d["junk"], d["other"],
    }
    second = reclaim(client).json()
    assert {item["digest"] for item in second["deleted"]} == {
        d["junk"], d["other"]
    }


def test_default_body_performs_real_reclamation(client):
    junk_d, junk_raw, _ = manifest("sha256:" + "c" * 64)
    ingest(client, junk_raw)

    body = reclaim(client).json()
    assert body["dryRun"] is False
    assert body["deletedCount"] == 1
    assert client.get(f"/manifests/{junk_d}").status_code == 404


def test_repeated_reclaim_is_a_noop(client):
    build_referenced_tree(client)
    assert reclaim(client).json()["deletedCount"] == 2
    second = reclaim(client).json()
    assert second["deletedCount"] == 0
    assert second["deletedBytes"] == 0
    assert second["remaining"] == 0


# ---------------------------------------------------------------------------
# limit: lexicographically smallest candidates first
# ---------------------------------------------------------------------------


def test_limit_deletes_smallest_digests_and_reports_remaining(client):
    d = build_referenced_tree(client)

    preview = reclaim(client, dryRun=True, limit=1).json()
    assert preview["remaining"] == 1
    candidates = sorted([d["junk"], d["other"]])
    assert [item["digest"] for item in preview["deleted"]] == [candidates[0]]
    # dryRun keeps both rows
    assert len(digests_in(client)) == 5

    real = reclaim(client, limit=1).json()
    assert [item["digest"] for item in real["deleted"]] == [candidates[0]]
    assert real["remaining"] == 1
    assert real["deletedCount"] == 1
    assert candidates[0] not in digests_in(client)
    assert candidates[1] in digests_in(client)

    tail = reclaim(client).json()
    assert [item["digest"] for item in tail["deleted"]] == [candidates[1]]
    assert tail["remaining"] == 0


def test_limit_larger_than_candidates_deletes_all(client):
    junk_d, junk_raw, _ = manifest("sha256:" + "c" * 64)
    ingest(client, junk_raw)
    body = reclaim(client, limit=10).json()
    assert body["deletedCount"] == 1
    assert body["remaining"] == 0


# ---------------------------------------------------------------------------
# Failure atomicity: any parse/resolution failure rolls everything back
# ---------------------------------------------------------------------------


def test_corrupt_reachable_index_aborts_and_deletes_nothing(client, repo):
    d = build_referenced_tree(client)
    conn = repo._connect()
    try:
        conn.execute(
            "UPDATE manifests SET doc = ? WHERE digest = ?",
            ("{not-json", d["index"]),
        )
        conn.commit()
    finally:
        conn.close()

    r = reclaim(client)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "RECLAIM_FAILED"
    assert body["error"]["details"]["digest"] == d["index"]

    # Rollback means neither the orphans nor anything else was removed.
    # repo.count() reads columns only, so the corrupt row does not break it.
    assert repo.count() == 5
    assert repo.get(d["junk"]) is not None


def test_malformed_child_descriptor_aborts_reclaim(client, repo):
    d = build_referenced_tree(client)
    bad_doc = {"schemaVersion": 2,
               "mediaType": "application/vnd.oci.image.index.v1+json",
               "manifests": [{"mediaType":
                              "application/vnd.oci.image.manifest.v1+json",
                              "size": 1}]}  # missing digest
    conn = repo._connect()
    try:
        conn.execute(
            "UPDATE manifests SET doc = ? WHERE digest = ?",
            (json.dumps(bad_doc), d["index"]),
        )
        conn.commit()
    finally:
        conn.close()

    r = reclaim(client)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RECLAIM_FAILED"
    assert repo.count() == 5
    assert repo.get(d["junk"]) is not None


def test_invalid_child_digest_aborts_reclaim(client, repo):
    d = build_referenced_tree(client)
    bad_doc = {"schemaVersion": 2,
               "mediaType": "application/vnd.oci.image.index.v1+json",
               "manifests": [{"digest": "not-a-digest"}]}
    conn = repo._connect()
    try:
        conn.execute(
            "UPDATE manifests SET doc = ? WHERE digest = ?",
            (json.dumps(bad_doc), d["index"]),
        )
        conn.commit()
    finally:
        conn.close()

    r = reclaim(client)
    assert r.status_code == 409
    details = r.json()["error"]["details"]
    assert details["digest"] == d["index"]
    assert details["child_digest"] == "not-a-digest"
    assert repo.count() == 5
    assert repo.get(d["junk"]) is not None


def test_dangling_child_reference_is_reported_not_fatal(client, repo):
    # An index whose child was never ingested: resolving that platform fails
    # with DIGEST_NOT_FOUND, but reclamation must not abort on it -- the
    # dangling reference leads to no stored document.
    missing = "sha256:" + "f" * 64
    idx_d, idx_raw, _ = index([child(missing, "linux", "amd64")])
    ingest(client, idx_raw)
    junk_d, junk_raw, _ = manifest("sha256:" + "3" * 64)
    ingest(client, junk_raw)

    # Promotion requires the platform to resolve, which it cannot here, so the
    # history root is seeded directly through the repository layer.
    repo.promote_channel("stable", idx_d, idx_d, [{"digest": missing}])

    r = reclaim(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["danglingReferences"] == [missing]
    assert {item["digest"] for item in body["deleted"]} == {junk_d}
    assert idx_d in digests_in(client)


# ---------------------------------------------------------------------------
# Promotions are blocked while the reclaim transaction is open
# ---------------------------------------------------------------------------


def test_promotion_blocks_while_reclaim_transaction_is_open(
    client, repo, monkeypatch
):
    d = build_referenced_tree(client)
    assert repo.get(d["junk"]) is not None

    # A second Repository over the same database file, like a second service
    # worker contending on SQLite's database-level write lock.
    second = repo.__class__(repo.db_path)

    imported = __import__("app.storage", fromlist=["compute_reachable"])
    entered = threading.Event()
    release = threading.Event()

    def slow_compute(conn, roots):
        result = real_compute(conn, roots)
        entered.set()
        # Hold the BEGIN IMMEDIATE write lock open for the duration.
        release.wait(timeout=5)
        return result

    real_compute = imported.compute_reachable
    monkeypatch.setattr(imported, "compute_reachable", slow_compute)

    outcome: list = []

    def sweep():
        outcome.append(repo.reclaim_manifests())

    t = threading.Thread(target=sweep)
    t.start()
    assert entered.wait(timeout=5)

    # Re-promoting the same root is legal (revision bumps); its
    # BEGIN IMMEDIATE must queue behind the open reclamation transaction.
    promoted = threading.Event()

    def do_promote():
        second.promote_channel(
            "stable", d["index"], d["index"], [{"digest": d["amd"]}],
            expected_digest=d["index"],
        )
        promoted.set()

    p = threading.Thread(target=do_promote)
    p.start()
    assert not promoted.wait(timeout=1.0), "promotion ran while GC held the lock"
    # While blocked, nothing has been deleted yet.
    assert second.get(d["junk"]) is not None

    release.set()
    t.join(timeout=5)
    p.join(timeout=35)
    assert promoted.is_set()

    # GC removed the two orphans; the queued promotion then serialized after
    # it and bumped the revision without a compare-and-swap conflict.
    assert len(outcome[0].deleted) == 2
    channel = second.get_channel("stable")
    assert channel.current_digest == d["index"]
    assert channel.revision == 2
    assert repo.get(d["junk"]) is None
    assert repo.get(d["index"]) is not None


def test_dry_run_also_holds_the_write_lock(client, repo, monkeypatch):
    build_referenced_tree(client)
    imported = __import__("app.storage", fromlist=["compute_reachable"])
    entered = threading.Event()
    release = threading.Event()

    def slow_compute(conn, roots):
        result = real_compute(conn, roots)
        entered.set()
        release.wait(timeout=5)
        return result

    real_compute = imported.compute_reachable
    monkeypatch.setattr(imported, "compute_reachable", slow_compute)

    t = threading.Thread(target=repo.reclaim_manifests,
                         kwargs={"dry_run": True})
    t.start()
    assert entered.wait(timeout=5)

    holder = repo.__class__(repo.db_path)
    lock_observed = threading.Event()

    def try_begin():
        conn = holder._connect()
        conn.execute("BEGIN IMMEDIATE")
        lock_observed.set()
        conn.rollback()
        conn.close()

    w = threading.Thread(target=try_begin)
    w.start()
    assert not lock_observed.wait(timeout=1.0)
    release.set()
    t.join(timeout=5)
    w.join(timeout=35)
    assert lock_observed.is_set()


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_reclaim_request_validation(client):
    cases = [
        (b"[]", "array body"),
        (b'{"dryRun": "yes"}', "non-bool dryRun"),
        (b'{"limit": 0}', "zero limit"),
        (b'{"limit": -3}', "negative limit"),
        (b'{"limit": "5"}', "string limit"),
        (b'{"limit": true}', "boolean limit"),
        (b"{broken", "malformed JSON"),
    ]
    for raw, label in cases:
        r = client.post("/maintenance/reclaim", content=raw,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 400, (label, r.status_code, r.text)
        assert r.json()["error"]["code"] == "INVALID_REQUEST", label


def test_empty_body_defaults_to_real_reclaim(client):
    junk_d, junk_raw, _ = manifest()
    ingest(client, junk_raw)
    r = client.post("/maintenance/reclaim")
    assert r.status_code == 200
    body = r.json()
    assert body["dryRun"] is False
    assert body["deletedCount"] == 1


def test_reclaim_response_shape(client):
    build_referenced_tree(client)
    body = reclaim(client, dryRun=True).json()
    assert set(body) == {
        "dryRun", "deleted", "deletedCount", "deletedBytes", "remaining",
        "reachable", "roots", "danglingReferences",
    }
    item = body["deleted"][0]
    assert set(item) == {"digest", "bytes"}
    assert isinstance(item["bytes"], int)
