"""Recursive resolution of a deployable image manifest from a root digest.

Starting at a stored OCI Image Index (or Manifest), the resolver walks child
descriptors selected by the client platform until it reaches an image
manifest.  Every error carries the digest chain traversed so far.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .errors import ErrorCode, OCIError
from .oci import (
    child_manifests,
    document_kind,
    is_index_media_type,
    is_manifest_media_type,
    validate_digest,
)
from .platform import (
    PlatformRequest,
    describe_candidate,
    descriptor_digest,
    descriptor_platform,
    select_candidates,
)
from .storage import StoredDocument

MAX_INDEX_HOPS = int(os.environ.get("OCI_MAX_INDEX_HOPS", "32"))


@dataclass(frozen=True)
class ResolveResult:
    digest: str
    media_type: str
    manifest: dict[str, Any]
    descriptor: dict[str, Any] | None
    platform: dict[str, Any] | None
    chain: list[str]
    index_hops: int


def _load(repo, digest: str, chain: list[str]) -> StoredDocument:
    stored = repo.get(digest)
    if stored is None:
        raise OCIError(
            ErrorCode.DIGEST_NOT_FOUND,
            f"Referenced digest has not been ingested: {digest}",
            {"digest": digest, "chain": list(chain)},
        )
    return stored


def _verify_descriptor_media_type(
    descriptor: dict[str, Any] | None,
    stored: StoredDocument,
    chain: list[str],
) -> None:
    """A descriptor's declared mediaType must agree with the target document."""
    if not descriptor:
        return
    declared = descriptor.get("mediaType")
    if not isinstance(declared, str) or not declared:
        # mediaType is optional on descriptors; nothing to cross-check.
        return
    if declared != stored.media_type:
        raise OCIError(
            ErrorCode.MEDIA_TYPE_MISMATCH,
            f"Descriptor for {stored.digest} declares mediaType {declared} "
            f"but the stored document is {stored.media_type}",
            {
                "digest": stored.digest,
                "expected_media_type": declared,
                "actual_media_type": stored.media_type,
                "chain": list(chain),
            },
        )
    if document_kind(stored.media_type) == "unknown":
        raise OCIError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"Digest {stored.digest} has unsupported mediaType {stored.media_type}",
            {"digest": stored.digest, "mediaType": stored.media_type},
        )


def _available_platforms(descriptors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    platforms: list[dict[str, Any]] = []
    for desc in descriptors:
        platform = descriptor_platform(desc)
        if platform is None:
            continue
        platforms.append(
            {
                "digest": descriptor_digest(desc),
                "platform": platform,
            }
        )
    return platforms


def resolve(
    repo, root_digest: str, request: PlatformRequest
) -> ResolveResult:
    root = validate_digest(root_digest)
    chain: list[str] = [root]
    selected_descriptor: dict[str, Any] | None = None
    hops = 0

    while True:
        current_digest = chain[-1]
        descriptor = selected_descriptor if hops > 0 else None
        stored = _load(repo, current_digest, chain)
        _verify_descriptor_media_type(descriptor, stored, chain)

        if is_manifest_media_type(stored.media_type):
            return ResolveResult(
                digest=stored.digest,
                media_type=stored.media_type,
                manifest=stored.doc,
                descriptor=descriptor,
                platform=descriptor_platform(descriptor) if descriptor else None,
                chain=list(chain),
                index_hops=hops,
            )

        if not is_index_media_type(stored.media_type):
            raise OCIError(
                ErrorCode.UNSUPPORTED_MEDIA_TYPE,
                f"Digest {stored.digest} has unsupported mediaType {stored.media_type}",
                {
                    "digest": stored.digest,
                    "mediaType": stored.media_type,
                    "chain": list(chain),
                },
            )

        # Current node is another index and ``hops`` index->child edges have
        # already been followed; traversing one more would exceed the limit.
        if hops >= MAX_INDEX_HOPS:
            raise OCIError(
                ErrorCode.DEPTH_EXCEEDED,
                f"Manifest nesting exceeded the limit of {MAX_INDEX_HOPS} index hops.",
                {"max_index_hops": MAX_INDEX_HOPS, "chain": list(chain)},
            )

        children = child_manifests(stored.doc)

        best, ranked = select_candidates(request, children)
        if best is None:
            if ranked:
                # Only reachable when the top rank is shared.
                tied = [
                    describe_candidate(desc, rank)
                    for desc, rank in ranked
                    if rank == ranked[0][1]
                ]
                raise OCIError(
                    ErrorCode.AMBIGUOUS_MATCH,
                    f"{len(tied)} candidates share the best platform match "
                    f"in index {current_digest}",
                    {
                        "request": request.to_dict(),
                        "index": current_digest,
                        "chain": list(chain),
                        "tied_candidates": tied,
                        "considered_candidates": [
                            describe_candidate(desc, rank) for desc, rank in ranked
                        ],
                    },
                )
            raise OCIError(
                ErrorCode.NO_MATCHING_PLATFORM,
                f"No manifest in index {current_digest} matches platform "
                f"{request.os}/{request.architecture}",
                {
                    "request": request.to_dict(),
                    "index": current_digest,
                    "chain": list(chain),
                    "available_platforms": _available_platforms(children),
                },
            )

        next_digest_raw = descriptor_digest(best)
        if not isinstance(next_digest_raw, str):
            raise OCIError(
                ErrorCode.INVALID_DOCUMENT,
                "Selected child descriptor is missing a string 'digest'.",
                {"descriptor": best, "chain": list(chain)},
            )
        next_digest = validate_digest(next_digest_raw)
        if next_digest in chain:
            raise OCIError(
                ErrorCode.CIRCULAR_REFERENCE,
                f"Circular index reference detected at {next_digest}",
                {
                    "digest": next_digest,
                    "chain": list(chain) + [next_digest],
                },
            )

        chain.append(next_digest)
        selected_descriptor = best
        hops += 1
