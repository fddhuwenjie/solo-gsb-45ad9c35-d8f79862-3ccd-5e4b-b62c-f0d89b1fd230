import json

from conftest import child, index, ingest, manifest, sha256_digest

from app import main as main_module


def resolve(client, root, os_name, arch, **extra):
    platform = {"os": os_name, "architecture": arch, **extra}
    return client.post("/resolve", json={"rootDigest": root, "platform": platform})


def test_ingest_and_get_roundtrip(client):
    d, raw, doc = manifest()
    digest = ingest(client, raw)

    r = client.get(f"/manifests/{digest}")
    assert r.status_code == 200
    assert r.json() == doc
    assert r.headers["Docker-Content-Digest"] == digest
    assert r.headers["Content-Type"] == "application/vnd.oci.image.manifest.v1+json"

    r = client.get("/manifests")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["documents"][0]["kind"] == "manifest"


def test_ingest_rejects_digest_mismatch(client):
    _, raw, _ = manifest()
    r = client.put("/manifests/sha256:" + "f" * 64, content=raw)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DIGEST"
    assert r.json()["error"]["details"]["actual_digest"] == sha256_digest(raw)


def test_ingest_rejects_bad_json(client):
    raw = b"{not json"
    r = client.put(f"/manifests/sha256:{'a' * 64}", content=raw)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DOCUMENT"


def test_ingest_rejects_shape_mismatch_media_type(client):
    # manifest shape (has config) but claims to be an index
    doc = {"schemaVersion": 2,
           "mediaType": "application/vnd.oci.image.index.v1+json",
           "config": {"digest": "sha256:" + "0" * 64}}
    raw = json.dumps(doc).encode()
    r = client.put(f"/manifests/{sha256_digest(raw)}", content=raw)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DOCUMENT"


def test_root_digest_not_found(client):
    r = resolve(client, "sha256:" + "9" * 64, "linux", "amd64")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "DIGEST_NOT_FOUND"
    assert body["error"]["details"]["chain"] == ["sha256:" + "9" * 64]


def test_simple_index_resolution_and_chain(client):
    m1_d, m1_raw, _ = manifest()
    m2_d, m2_raw, _ = manifest("sha256:" + "a" * 64)
    ingest(client, m1_raw)
    ingest(client, m2_raw)

    idx_d, idx_raw, _ = index([
        child(m1_d, "linux", "arm64"),
        child(m2_d, "linux", "amd64"),
    ])
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "amd64")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["digest"] == m2_d
    assert body["mediaType"] == "application/vnd.oci.image.manifest.v1+json"
    assert body["chain"] == [idx_d, m2_d]
    assert body["indexHops"] == 1
    assert body["platform"] == {"os": "linux", "architecture": "amd64"}
    assert body["manifest"]["config"]["digest"] == "sha256:" + "a" * 64


def test_arch_and_os_require_exact_match(client):
    m_d, m_raw, _ = manifest()
    ingest(client, m_raw)
    idx_d, idx_raw, _ = index([child(m_d, "linux", "arm64")])
    ingest(client, idx_raw)

    assert resolve(client, idx_d, "windows", "arm64").status_code == 404
    assert resolve(client, idx_d, "linux", "amd64").status_code == 404
    assert resolve(client, idx_d, "linux", "ARM64").status_code == 404


def test_variant_exact_beats_empty_fallback(client):
    # arm64-v7 specific manifest, plus a generic arm64 manifest
    mv_d, mv_raw, _ = manifest("sha256:" + "b" * 64)
    mg_d, mg_raw, _ = manifest("sha256:" + "c" * 64)
    ingest(client, mv_raw)
    ingest(client, mg_raw)

    idx_d, idx_raw, _ = index([
        child(mg_d, "linux", "arm64"),            # no variant
        child(mv_d, "linux", "arm64", variant="v7"),
    ])
    ingest(client, idx_raw)

    # request without variant: both candidates are compatible; the generic
    # (empty variant) one wins over the v7-specific one via empty fallback.
    r = resolve(client, idx_d, "linux", "arm64")
    assert r.status_code == 200
    assert r.json()["digest"] == mg_d

    # request with v7: exact match beats the empty-fallback candidate
    r = resolve(client, idx_d, "linux", "arm64", variant="v7")
    assert r.status_code == 200
    assert r.json()["digest"] == mv_d
    assert r.json()["chain"] == [idx_d, mv_d]


def test_os_version_exact_beats_empty_fallback(client):
    exact_d, exact_raw, _ = manifest("sha256:" + "2" * 64)
    generic_d, generic_raw, _ = manifest("sha256:" + "3" * 64)
    ingest(client, exact_raw)
    ingest(client, generic_raw)

    idx_d, idx_raw, _ = index([
        child(generic_d, "linux", "amd64"),
        child(exact_d, "linux", "amd64", os_version="10.0.20348"),
    ])
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "amd64", **{"os.version": "10.0.20348"})
    assert r.status_code == 200
    assert r.json()["digest"] == exact_d

    # requesting a different non-empty version cannot fall back to that
    # candidate; the generic (empty) one remains.
    r = resolve(client, idx_d, "linux", "amd64", **{"os.version": "9.0"})
    assert r.status_code == 200
    assert r.json()["digest"] == generic_d


def test_variant_ranks_ahead_of_os_version(client):
    # A: variant exact + os.version empty; B: variant empty + os.version exact
    a_d, a_raw, _ = manifest("sha256:" + "4" * 64)
    b_d, b_raw, _ = manifest("sha256:" + "5" * 64)
    ingest(client, a_raw)
    ingest(client, b_raw)

    idx_d, idx_raw, _ = index([
        child(a_d, "linux", "arm64", variant="v7"),
        child(b_d, "linux", "arm64", os_version="1.0"),
    ])
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "arm64", variant="v7", **{"os.version": "1.0"})
    assert r.status_code == 200
    assert r.json()["digest"] == a_d  # variant match wins the ordering


def test_os_features_must_all_be_satisfied(client):
    with_f_d, with_f_raw, _ = manifest("sha256:" + "6" * 64)
    plain_d, plain_raw, _ = manifest("sha256:" + "7" * 64)
    ingest(client, with_f_raw)
    ingest(client, plain_raw)

    idx_d, idx_raw, _ = index([
        child(plain_d, "linux", "amd64"),
        child(with_f_d, "linux", "amd64", os_features=["foo", "bar", "baz"]),
    ])
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "amd64", **{"os.features": ["foo", "bar"]})
    assert r.status_code == 200
    assert r.json()["digest"] == with_f_d  # extra candidate features are allowed

    # 'missing' feature satisfied by neither entry -> no match
    r = resolve(client, idx_d, "linux", "amd64", **{"os.features": ["foo", "nope"]})
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "NO_MATCHING_PLATFORM"
    available = body["error"]["details"]["available_platforms"]
    assert len(available) == 2


def test_ambiguous_candidates_return_details(client):
    d1, raw1, _ = manifest("sha256:" + "1" * 64)
    d2, raw2, _ = manifest("sha256:" + "2" * 64)
    ingest(client, raw1)
    ingest(client, raw2)

    idx_d, idx_raw, _ = index([
        child(d1, "linux", "amd64"),
        child(d2, "linux", "amd64"),
    ])
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "amd64")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "AMBIGUOUS_MATCH"
    tied = body["error"]["details"]["tied_candidates"]
    assert {c["digest"] for c in tied} == {d1, d2}
    assert body["error"]["details"]["chain"] == [idx_d]


def test_nested_index_recursion_and_chain(client):
    leaf_d, leaf_raw, _ = manifest("sha256:" + "8" * 64)
    ingest(client, leaf_raw)

    inner_d, inner_raw, _ = index([child(leaf_d, "linux", "amd64")])
    ingest(client, inner_raw)
    outer_d, outer_raw, _ = index([child(
        inner_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, outer_raw)

    r = resolve(client, outer_d, "linux", "amd64")
    assert r.status_code == 200
    body = r.json()
    assert body["digest"] == leaf_d
    assert body["chain"] == [outer_d, inner_d, leaf_d]
    assert body["indexHops"] == 2


def test_missing_child_manifest_reports_chain(client):
    missing = "sha256:" + "b" * 64
    idx_d, idx_raw, _ = index([child(missing, "linux", "amd64")])
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "amd64")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "DIGEST_NOT_FOUND"
    assert body["error"]["details"]["digest"] == missing
    assert body["error"]["details"]["chain"] == [idx_d, missing]


def test_circular_index_reference(client, repo):
    # Two indexes referencing each other form a 2-node cycle.  Such a cycle
    # cannot be produced through digest-verified PUTs (finding a SHA-256
    # fixed point is infeasible), so the documents are written under chosen
    # digests directly through the repository layer to exercise the guard.
    index_media = "application/vnd.oci.image.index.v1+json"
    a_digest = "sha256:" + "a1" * 32
    b_digest = "sha256:" + "b2" * 32

    a_doc = {
        "schemaVersion": 2,
        "mediaType": index_media,
        "manifests": [
            child(b_digest, "linux", "amd64", media_type=index_media)
        ],
    }
    b_doc = {
        "schemaVersion": 2,
        "mediaType": index_media,
        "manifests": [
            child(a_digest, "linux", "amd64", media_type=index_media)
        ],
    }
    repo.upsert(a_digest, index_media, a_doc, len(json.dumps(a_doc)))
    repo.upsert(b_digest, index_media, b_doc, len(json.dumps(b_doc)))

    r = resolve(client, a_digest, "linux", "amd64")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "CIRCULAR_REFERENCE"
    assert body["error"]["details"]["digest"] == a_digest
    assert body["error"]["details"]["chain"] == [a_digest, b_digest, a_digest]


def test_depth_limit(client, monkeypatch):
    monkeypatch.setattr("app.resolver.MAX_INDEX_HOPS", 2)

    leaf_d, leaf_raw, _ = manifest("sha256:" + "c" * 64)
    ingest(client, leaf_raw)
    # i3 -> leaf would require a 3rd hop from i2 and must never be loaded.
    i3_d, i3_raw, _ = index([child(leaf_d, "linux", "amd64")])
    ingest(client, i3_raw)
    i2_d, i2_raw, _ = index([child(
        i3_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, i2_raw)
    i1_d, i1_raw, _ = index([child(
        i2_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, i1_raw)
    i0_d, i0_raw, _ = index([child(
        i1_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, i0_raw)

    r = resolve(client, i0_d, "linux", "amd64")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "DEPTH_EXCEEDED"
    assert body["error"]["details"]["max_index_hops"] == 2
    # Two edges were followed (i0->i1->i2); i3 is not visited.
    assert body["error"]["details"]["chain"] == [i0_d, i1_d, i2_d]


def test_depth_boundary_allows_exactly_max_hops(client, monkeypatch):
    monkeypatch.setattr("app.resolver.MAX_INDEX_HOPS", 2)

    leaf_d, leaf_raw, _ = manifest("sha256:" + "d" * 64)
    ingest(client, leaf_raw)
    i1_d, i1_raw, _ = index([child(leaf_d, "linux", "amd64")])
    ingest(client, i1_raw)
    i0_d, i0_raw, _ = index([child(
        i1_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, i0_raw)

    r = resolve(client, i0_d, "linux", "amd64")
    assert r.status_code == 200
    body = r.json()
    assert body["digest"] == leaf_d
    assert body["chain"] == [i0_d, i1_d, leaf_d]
    assert body["indexHops"] == 2


def test_descriptor_media_type_mismatch(client):
    m_d, m_raw, _ = manifest()
    ingest(client, m_raw)
    # descriptor claims index, stored document is a manifest
    idx_d, idx_raw, _ = index([
        child(m_d, "linux", "amd64",
              media_type="application/vnd.oci.image.index.v1+json")
    ])
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "amd64")
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "MEDIA_TYPE_MISMATCH"
    details = body["error"]["details"]
    assert details["expected_media_type"] == "application/vnd.oci.image.index.v1+json"
    assert details["actual_media_type"] == "application/vnd.oci.image.manifest.v1+json"
    assert details["chain"] == [idx_d, m_d]


def test_docker_media_types_supported(client):
    m_d, m_raw, _ = manifest(
        media_type="application/vnd.docker.distribution.manifest.v2+json")
    ingest(client, m_raw)
    idx_d, idx_raw, _ = index(
        [child(m_d, "linux", "amd64",
               media_type="application/vnd.docker.distribution.manifest.v2+json")],
        media_type="application/vnd.docker.distribution.manifest.list.v2+json",
    )
    ingest(client, idx_raw)

    r = resolve(client, idx_d, "linux", "amd64")
    assert r.status_code == 200
    assert r.json()["digest"] == m_d


def test_root_can_be_a_manifest_directly(client):
    m_d, m_raw, _ = manifest()
    ingest(client, m_raw)
    r = resolve(client, m_d, "linux", "amd64")
    assert r.status_code == 200
    assert r.json()["digest"] == m_d
    assert r.json()["chain"] == [m_d]
    assert r.json()["indexHops"] == 0
    assert r.json()["platform"] is None


def test_ingest_idempotent(client):
    m_d, m_raw, _ = manifest()
    r1 = client.put(f"/manifests/{m_d}", content=m_raw)
    r2 = client.put(f"/manifests/{m_d}", content=m_raw)
    assert r1.json()["created"] is True
    assert r2.json()["created"] is False
    assert client.get("/manifests").json()["count"] == 1


def test_head_and_bare_hex_digest(client):
    m_d, m_raw, _ = manifest()
    ingest(client, m_raw)
    bare = m_d.split(":", 1)[1]
    r = client.head(f"/manifests/{bare}")
    assert r.status_code == 200
    assert r.headers["Docker-Content-Digest"] == m_d
    r = client.get(f"/manifests/{bare}")
    assert r.status_code == 200


def test_invalid_request_fields(client):
    r = client.post("/resolve", json={"platform": {"os": "linux"}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    r = client.post("/resolve", json={"rootDigest": "sha256:" + "0" * 64,
                                      "platform": {"os": "linux",
                                                   "architecture": "amd64",
                                                   "os.features": ["ok", 3]}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_ambiguous_when_two_empty_fallback_candidates(client):
    d1, raw1, _ = manifest("sha256:" + "a" * 64)
    d2, raw2, _ = manifest("sha256:" + "b" * 64)
    ingest(client, raw1)
    ingest(client, raw2)
    idx_d, idx_raw, _ = index([
        child(d1, "linux", "arm64", variant="v7"),
        child(d2, "linux", "arm64", variant="v8"),
    ])
    ingest(client, idx_raw)
    # no variant requested: both are merely compatible-extra and tie
    r = resolve(client, idx_d, "linux", "arm64")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "AMBIGUOUS_MATCH"
    tied = r.json()["error"]["details"]["tied_candidates"]
    assert {c["digest"] for c in tied} == {d1, d2}
    assert tied[0]["rank"]["variant"] == "compatible-extra"
