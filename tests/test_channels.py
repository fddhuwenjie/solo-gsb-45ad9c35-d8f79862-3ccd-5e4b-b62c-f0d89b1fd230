import threading

from conftest import child, index, ingest, manifest

from app.channels import encode_cursor
from app.storage import ChannelConflict, Repository


def promote(client, name, root, platforms, **extra):
    body = {"rootDigest": root, "platforms": platforms, **extra}
    return client.post(f"/channels/{name}/promote", json=body)


def plat(os_name="linux", arch="amd64", **extra):
    return {"os": os_name, "architecture": arch, **extra}


def build_multiarch_index(client):
    """Index -> (amd64 manifest, arm64 manifest)."""
    m_amd_d, m_amd_raw, _ = manifest("sha256:" + "a" * 64)
    m_arm_d, m_arm_raw, _ = manifest("sha256:" + "b" * 64)
    ingest(client, m_amd_raw)
    ingest(client, m_arm_raw)
    idx_d, idx_raw, _ = index([
        child(m_amd_d, "linux", "amd64"),
        child(m_arm_d, "linux", "arm64"),
    ])
    ingest(client, idx_raw)
    return idx_d, m_amd_d, m_arm_d


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_first_promotion_creates_channel_with_revision_one(client):
    idx_d, m_amd_d, m_arm_d = build_multiarch_index(client)

    r = promote(client, "stable", idx_d, [plat(), plat(arch="arm64")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "stable"
    assert body["sequence"] == 1
    assert body["rootDigest"] == idx_d
    assert body["newDigest"] == idx_d
    assert body["oldDigest"] is None
    assert len(body["results"]) == 2

    amd_result, arm_result = body["results"]
    assert amd_result["digest"] == m_amd_d
    assert amd_result["chain"] == [idx_d, m_amd_d]
    assert amd_result["indexHops"] == 1
    assert amd_result["request"] == plat()
    assert amd_result["platform"] == plat()
    assert amd_result["descriptor"]["digest"] == m_amd_d
    assert arm_result["digest"] == m_arm_d


def test_current_value_endpoint_reports_latest_pointer(client):
    idx_d, _, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])

    r = client.get("/channels/stable")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "name": "stable",
        "currentDigest": idx_d,
        "sequence": 1,
        "updatedAt": body["updatedAt"],
    }
    assert isinstance(body["updatedAt"], str) and body["updatedAt"]


def test_list_channels_sorted_by_name(client):
    idx_d, _, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])
    promote(client, "beta", idx_d, [plat()])

    r = client.get("/channels")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert [c["name"] for c in body["channels"]] == ["beta", "stable"]
    assert body["channels"][1]["sequence"] == 1


def test_repromote_bumps_revision_and_records_old_digest(client):
    idx_d, _, _ = build_multiarch_index(client)
    other_d, other_raw, _ = manifest("sha256:" + "c" * 64)
    ingest(client, other_raw)
    other_idx_d, other_idx_raw, _ = index([child(other_d, "linux", "amd64")])
    ingest(client, other_idx_raw)

    r1 = promote(client, "stable", idx_d, [plat()])
    assert r1.json()["sequence"] == 1

    r2 = promote(client, "stable", other_idx_d, [plat()])
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["sequence"] == 2
    assert body["oldDigest"] == idx_d
    assert body["newDigest"] == other_idx_d
    assert body["rootDigest"] == other_idx_d

    current = client.get("/channels/stable").json()
    assert current["currentDigest"] == other_idx_d
    assert current["sequence"] == 2


# ---------------------------------------------------------------------------
# Atomicity: failures leave the channel untouched
# ---------------------------------------------------------------------------


def test_any_platform_failure_aborts_promotion_with_channel_unchanged(client):
    idx_d, m_amd_d, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])

    # amd64 resolves but ppc64le does not exist in the index.
    r = promote(client, "stable", idx_d, [plat(), plat(arch="ppc64le")])
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "PROMOTION_FAILED"
    details = body["error"]["details"]
    assert details["channel"] == "stable"
    assert details["rootDigest"] == idx_d

    # One successful resolution plus one platform-level diagnostic.
    assert len(details["results"]) == 2
    assert len(details["failures"]) == 1
    failure = details["failures"][0]
    assert failure["request"] == plat(arch="ppc64le")
    assert failure["error"]["code"] == "NO_MATCHING_PLATFORM"
    assert failure["error"]["details"]["index"] == idx_d
    assert failure["error"]["details"]["chain"] == [idx_d]
    success = next(
        item for item in details["results"] if "error" not in item
    )
    assert success["digest"] == m_amd_d

    # Channel pointer and revision must not have moved.
    current = client.get("/channels/stable").json()
    assert current["currentDigest"] == idx_d
    assert current["sequence"] == 1

    events = client.get("/channels/stable/events").json()
    assert len(events["items"]) == 1
    assert events["items"][0]["sequence"] == 1


def test_all_platform_failures_are_reported_together(client):
    idx_d, _, _ = build_multiarch_index(client)
    r = promote(
        client,
        "stable",
        idx_d,
        [plat(arch="ppc64le"), plat(os_name="windows", arch="arm64")],
    )
    assert r.status_code == 409
    failures = r.json()["error"]["details"]["failures"]
    assert len(failures) == 2
    assert all(f["error"]["code"] == "NO_MATCHING_PLATFORM" for f in failures)
    assert client.get("/channels/stable").status_code == 404


def test_promotion_reports_ambiguity_and_missing_digest_per_platform(client):
    # Ambiguous root: two identical amd64 children.
    d1, raw1, _ = manifest("sha256:" + "1" * 64)
    d2, raw2, _ = manifest("sha256:" + "2" * 64)
    ingest(client, raw1)
    ingest(client, raw2)
    ambig_d, ambig_raw, _ = index([
        child(d1, "linux", "amd64"),
        child(d2, "linux", "amd64"),
    ])
    ingest(client, ambig_raw)

    r = promote(client, "stable", ambig_d, [plat()])
    assert r.status_code == 409
    failure = r.json()["error"]["details"]["failures"][0]
    assert failure["error"]["code"] == "AMBIGUOUS_MATCH"
    assert failure["error"]["details"]["chain"] == [ambig_d]

    # A digest missing from the chain is likewise reported platform-locally.
    missing = "sha256:" + "f" * 64
    missing_idx_d, missing_idx_raw, _ = index([
        child(missing, "linux", "arm64")
    ])
    ingest(client, missing_idx_raw)
    r = promote(client, "stable", missing_idx_d, [plat(arch="arm64")])
    assert r.status_code == 409
    failure = r.json()["error"]["details"]["failures"][0]
    assert failure["error"]["code"] == "DIGEST_NOT_FOUND"
    assert failure["error"]["details"]["chain"] == [missing_idx_d, missing]
    assert client.get("/channels/stable").status_code == 404


def test_root_digest_not_ingested_reports_platform_diagnostics(client):
    r = promote(client, "stable", "sha256:" + "9" * 64, [plat(), plat(arch="arm64")])
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "PROMOTION_FAILED"
    failures = body["error"]["details"]["failures"]
    assert len(failures) == 2
    assert all(f["error"]["code"] == "DIGEST_NOT_FOUND" for f in failures)
    assert failures[0]["error"]["details"]["chain"] == ["sha256:" + "9" * 64]
    assert client.get("/channels/stable").status_code == 404


# ---------------------------------------------------------------------------
# Compare-and-swap
# ---------------------------------------------------------------------------


def test_expected_digest_must_match_current_value(client):
    idx_d, _, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])

    stale = "sha256:" + "0" * 64
    r = promote(client, "stable", idx_d, [plat()], expectedDigest=stale)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "PROMOTION_CONFLICT"
    details = body["error"]["details"]
    assert details["channel"] == "stable"
    assert details["expectedDigest"] == stale
    assert details["currentDigest"] == idx_d

    # Stale expectation must not have written anything.
    current = client.get("/channels/stable").json()
    assert current["currentDigest"] == idx_d
    assert current["sequence"] == 1


def test_matching_expected_digest_allows_promotion(client):
    idx_d, _, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])

    r = promote(client, "stable", idx_d, [plat()], expectedDigest=idx_d)
    assert r.status_code == 200
    assert r.json()["sequence"] == 2


def test_expected_digest_blocks_first_promotion_on_new_channel(client):
    idx_d, _, _ = build_multiarch_index(client)
    r = promote(
        client,
        "stable",
        idx_d,
        [plat()],
        expectedDigest="sha256:" + "1" * 64,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PROMOTION_CONFLICT"
    assert r.json()["error"]["details"]["currentDigest"] is None
    assert client.get("/channels/stable").status_code == 404


def test_concurrent_promotions_cas_serializes(tmp_path):
    # Two threads promote the same channel concurrently with an expectation
    # pinned to the (identical) initial state; exactly one must win and the
    # ledger must stay gap-free.
    repo = Repository(str(tmp_path / "cas.db"))
    idx_d = "sha256:" + "a" * 64
    platforms = [{"request": {"os": "linux", "architecture": "amd64"},
                  "digest": idx_d, "chain": [idx_d]}]

    results: list[Exception | int] = []

    def worker() -> None:
        try:
            event = repo.promote_channel(
                name="stable",
                root_digest=idx_d,
                new_digest=idx_d,
                platforms=platforms,
                expected_digest=None,
            )
            results.append(event.revision)
        except ChannelConflict as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    channel = repo.get_channel("stable")
    assert channel is not None
    assert channel.revision == 2
    revisions = sorted(
        event.revision for event in repo.list_events("stable", limit=100)
    )
    assert revisions == [1, 2]


def test_concurrent_promotions_with_stale_expectation_one_conflicts(tmp_path):
    repo = Repository(str(tmp_path / "cas2.db"))
    d1 = "sha256:" + "a" * 64
    d2 = "sha256:" + "b" * 64
    platforms = [{"digest": d1}]

    # Seed revision 1; afterwards two threads race while both pinning their
    # expectation to d1.  Exactly one can win (moving to revision 2); the
    # loser must observe the moved pointer and get a conflict.
    repo.promote_channel("stable", d1, d1, platforms)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def winner() -> None:
        barrier.wait()
        try:
            repo.promote_channel(
                "stable", d2, d2, platforms, expected_digest=d1
            )
            outcomes.append("ok")
        except ChannelConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=winner) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    channel = repo.get_channel("stable")
    assert channel.revision == 2
    assert channel.current_digest == d2
    conflict = next(
        event for event in repo.list_events("stable", limit=100)
        if event.revision == 2
    )
    assert conflict.new_digest == d2 and conflict.old_digest == d1


# ---------------------------------------------------------------------------
# Immutable ledger + cursor history
# ---------------------------------------------------------------------------


def test_event_history_is_immutable_and_newest_first(client):
    idx_d, _, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])
    promote(client, "stable", idx_d, [plat(), plat(arch="arm64")])
    promote(client, "stable", idx_d, [plat(arch="arm64")])

    r = client.get("/channels/stable/events")
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "stable"
    assert body["nextCursor"] is None
    sequences = [item["sequence"] for item in body["items"]]
    assert sequences == [3, 2, 1]

    first = body["items"][-1]
    assert first["oldDigest"] is None
    assert first["newDigest"] == idx_d
    assert len(first["platforms"]) == 1
    second = body["items"][1]
    assert second["oldDigest"] == idx_d
    assert len(second["platforms"]) == 2


def test_history_cursor_pagination(client):
    idx_d, _, _ = build_multiarch_index(client)
    for _ in range(5):
        promote(client, "stable", idx_d, [plat()])

    page1 = client.get("/channels/stable/events?limit=2").json()
    assert [i["sequence"] for i in page1["items"]] == [5, 4]
    assert page1["nextCursor"]

    page2 = client.get(
        f"/channels/stable/events?limit=2&cursor={page1['nextCursor']}"
    ).json()
    assert [i["sequence"] for i in page2["items"]] == [3, 2]

    page3 = client.get(
        f"/channels/stable/events?limit=2&cursor={page2['nextCursor']}"
    ).json()
    assert [i["sequence"] for i in page3["items"]] == [1]
    assert page3["nextCursor"] is None

    # A full final page (4 events, page size 2) must not offer a cursor.
    other = "even"
    for _ in range(4):
        promote(client, other, idx_d, [plat()])
    p1 = client.get(f"/channels/{other}/events?limit=2").json()
    assert [i["sequence"] for i in p1["items"]] == [4, 3]
    p2 = client.get(
        f"/channels/{other}/events?limit=2&cursor={p1['nextCursor']}"
    ).json()
    assert [i["sequence"] for i in p2["items"]] == [2, 1]
    assert p2["nextCursor"] is None

    # Following the last cursor yields an empty page, not an error.
    p3 = client.get(
        f"/channels/{other}/events?limit=2&cursor={encode_cursor(other, 1)}"
    ).json()
    assert p3["items"] == []
    assert p3["nextCursor"] is None


def test_history_rejects_bad_and_foreign_cursor(client):
    idx_d, _, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])
    promote(client, "beta", idx_d, [plat()])

    r = client.get("/channels/stable/events?cursor=not-base64!!!")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CURSOR"

    # A cursor minted for another channel must be rejected.
    r = client.get(
        f"/channels/stable/events?cursor={encode_cursor('beta', 1)}"
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CURSOR"


def test_event_platforms_carry_full_resolution_details(client):
    idx_d, m_amd_d, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d,
            [plat(variant=None), plat(arch="arm64")])

    item = client.get("/channels/stable/events").json()["items"][0]
    recorded = item["platforms"]
    assert recorded[0]["digest"] == m_amd_d
    assert recorded[0]["mediaType"] == \
        "application/vnd.oci.image.manifest.v1+json"
    assert recorded[0]["chain"] == [idx_d, m_amd_d]
    assert recorded[0]["descriptor"]["platform"] == plat()


# ---------------------------------------------------------------------------
# Validation and 404s
# ---------------------------------------------------------------------------


def test_unknown_channel_returns_404(client):
    r = client.get("/channels/ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CHANNEL_NOT_FOUND"

    r = client.get("/channels/ghost/events")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CHANNEL_NOT_FOUND"


def test_invalid_channel_names_rejected(client):
    idx_d, _, _ = build_multiarch_index(client)
    # Names that reach the handler but violate the slug rules.  (Names with
    # '/' or that are empty cannot match the path segment at all.)
    for bad in ["a b", "a" * 65, "-lead", ".lead", "_lead", "under_score!"]:
        r = promote(client, bad, idx_d, [plat()])
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert r.json()["error"]["code"] == "INVALID_CHANNEL"


def test_promote_validates_payload(client):
    idx_d, _, _ = build_multiarch_index(client)

    r = promote(client, "stable", idx_d, [])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    r = promote(client, "stable", idx_d, [plat()] * 33)
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["max_platforms"] == 32

    r = client.post("/channels/stable/promote",
                    json={"rootDigest": idx_d,
                          "platforms": [{"os": "linux"}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"

    r = client.post("/channels/stable/promote",
                    json={"rootDigest": "not-a-digest", "platforms": [plat()]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DIGEST"

    r = client.post("/channels/stable/promote",
                    json={"rootDigest": idx_d, "platforms": [plat()],
                          "expectedDigest": "nonsense"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DIGEST"


def test_bare_hex_digest_accepted_on_promote(client):
    idx_d, _, _ = build_multiarch_index(client)
    bare = idx_d.split(":", 1)[1]
    r = promote(client, "stable", bare, [plat()])
    assert r.status_code == 200
    assert r.json()["newDigest"] == idx_d


def test_promote_accepts_docker_media_types(client):
    m_d, m_raw, _ = manifest(
        media_type="application/vnd.docker.distribution.manifest.v2+json")
    ingest(client, m_raw)
    idx_d, idx_raw, _ = index(
        [child(m_d, "linux", "amd64",
               media_type="application/vnd.docker.distribution.manifest.v2+json")],
        media_type="application/vnd.docker.distribution.manifest.list.v2+json",
    )
    ingest(client, idx_raw)

    r = promote(client, "stable", idx_d, [plat()])
    assert r.status_code == 200
    assert r.json()["results"][0]["digest"] == m_d


def test_nested_index_chain_recorded_in_promotion(client):
    leaf_d, leaf_raw, _ = manifest("sha256:" + "8" * 64)
    ingest(client, leaf_raw)
    inner_d, inner_raw, _ = index([child(leaf_d, "linux", "amd64")])
    ingest(client, inner_raw)
    outer_d, outer_raw, _ = index([child(
        inner_d, "linux", "amd64",
        media_type="application/vnd.oci.image.index.v1+json")])
    ingest(client, outer_raw)

    r = promote(client, "stable", outer_d, [plat()])
    assert r.status_code == 200
    result = r.json()["results"][0]
    assert result["digest"] == leaf_d
    assert result["chain"] == [outer_d, inner_d, leaf_d]
    assert result["indexHops"] == 2


def test_duplicate_platform_entries_are_both_recorded(client):
    idx_d, m_amd_d, _ = build_multiarch_index(client)
    r = promote(client, "stable", idx_d, [plat(), plat()])
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 2
    assert all(item["digest"] == m_amd_d for item in results)


def test_history_limit_query_validation(client):
    idx_d, _, _ = build_multiarch_index(client)
    promote(client, "stable", idx_d, [plat()])

    assert client.get("/channels/stable/events?limit=0").status_code == 422
    assert client.get("/channels/stable/events?limit=101").status_code == 422
