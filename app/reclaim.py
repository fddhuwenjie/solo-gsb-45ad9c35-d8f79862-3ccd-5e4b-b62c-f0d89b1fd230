"""Reachability analysis for manifest storage reclamation.

A stored manifest document is *reachable* when it is itself a GC root or when
it can be reached by recursively following the ``manifests`` descriptors of an
reachable OCI Image Index (or Docker manifest list).  The roots are:

* every channel's current pointer (``channels.current_digest``);
* every promotion ledger entry's ``root_digest`` (the full promotion history).

Documents that are not reachable are reclamation candidates: no existing
channel can resolve through them.

Traversal failures are deliberately strict.  A malformed stored index (bad
JSON, a non-object ``manifests`` entry, an invalid child digest) aborts the
whole reclamation so that an operator notices database corruption instead of
silently deciding reachability from unparseable data.  A child (or root)
digest that is simply not stored is not a parsing failure: such a *dangling*
reference cannot lead to a stored document, so it is skipped and reported.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import ErrorCode, OCIError
from .oci import INDEX_MEDIA_TYPES, validate_digest


def _fail(message: str, details: dict[str, Any]) -> OCIError:
    return OCIError(ErrorCode.RECLAIM_FAILED, message, details)


def parse_stored_document(row, *, digest: str) -> dict[str, Any]:
    """JSON-decode one stored document, failing the reclamation if corrupt.

    ``row`` must expose the stored JSON text as column ``doc``.
    """
    raw_doc = row["doc"]
    try:
        doc = json.loads(raw_doc)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _fail(
            f"Stored document for {digest} is not valid JSON; reclamation "
            "was aborted without deleting anything.",
            {"digest": digest, "cause": str(exc)},
        )
    if not isinstance(doc, dict):
        raise _fail(
            f"Stored document for {digest} is not a JSON object; reclamation "
            "was aborted without deleting anything.",
            {"digest": digest, "actual_type": type(doc).__name__},
        )
    return doc


def child_digest(descriptor: Any, *, parent: str) -> str:
    """Extract and validate a child descriptor's ``digest``.

    A descriptor that is not an object, lacks a string ``digest``, or carries a
    malformed digest is a stored-graph parsing error and aborts reclamation.
    """
    if not isinstance(descriptor, dict):
        raise _fail(
            f"Index {parent} contains a manifests entry that is not an "
            "object; reclamation was aborted.",
            {"digest": parent, "entry": descriptor},
        )
    value = descriptor.get("digest")
    if not isinstance(value, str) or not value:
        raise _fail(
            f"Index {parent} contains a manifests entry without a string "
            "'digest'; reclamation was aborted.",
            {"digest": parent, "descriptor": descriptor},
        )
    try:
        return validate_digest(value)
    except OCIError as exc:
        raise _fail(
            f"Index {parent} references a child with an invalid digest "
            f"{value!r}; reclamation was aborted.",
            {"digest": parent, "child_digest": value, "cause": exc.code},
        )


def compute_reachable(
    conn, roots: list[str]
) -> tuple[set[str], list[str]]:
    """Walk stored Index documents from ``roots``.

    Returns ``(reachable, dangling)`` where ``reachable`` is the set of
    digests of stored documents reachable from any root and ``dangling`` lists
    referenced (and well-formed) digests that are not stored.  Reads happen on
    ``conn`` inside the caller's write transaction so the snapshot is stable.
    """
    reachable: set[str] = set()
    dangling: set[str] = set()
    pending: list[str] = list(dict.fromkeys(roots))  # de-duplicate, keep order

    while pending:
        digest = pending.pop()
        if digest in reachable or digest in dangling:
            continue
        row = conn.execute(
            "SELECT media_type, doc FROM manifests WHERE digest = ?",
            (digest,),
        ).fetchone()
        if row is None:
            # A well-formed reference to a document that was never ingested
            # (or already removed) cannot make anything stored reachable.
            dangling.add(digest)
            continue
        reachable.add(digest)

        if row["media_type"] not in INDEX_MEDIA_TYPES:
            # Image manifests are leaves: only Index documents point further.
            continue

        doc = parse_stored_document(row, digest=digest)
        descriptors = doc.get("manifests")
        if not isinstance(descriptors, list):
            raise _fail(
                f"Stored index {digest} has no 'manifests' array; "
                "reclamation was aborted.",
                {"digest": digest},
            )
        for descriptor in descriptors:
            child = child_digest(descriptor, parent=digest)
            if child not in reachable and child not in dangling:
                pending.append(child)

    return reachable, sorted(dangling)
