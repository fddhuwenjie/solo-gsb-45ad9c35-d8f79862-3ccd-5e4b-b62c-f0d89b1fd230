"""Platform matching and candidate ranking.

The rules follow the OCI image index platform selection semantics (as
implemented by containerd / ``go-digest`` consumers):

* ``os`` and ``architecture`` must match exactly;
* an exact ``variant`` / ``os.version`` match beats falling back to a
  candidate that leaves the value empty;
* every requested ``os.features`` entry must be present on the candidate
  (the candidate may provide additional features);
* ``variant`` is ranked before ``os.version``.

When more than one candidate shares the best rank the match is ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Rank values within a single field:
#   an exact requested value beats an absent/empty fallback value, which in
#   turn beats an unrequested extra value that merely stays compatible.
_EXACT = 3
_EMPTY = 2
_COMPATIBLE = 1

_VARIANT_LABELS = {
    _EXACT: "exact",
    _EMPTY: "fallback-empty",
    _COMPATIBLE: "compatible-extra",
}
_VERSION_LABELS = dict(_VARIANT_LABELS)


@dataclass(frozen=True)
class PlatformRequest:
    os: str
    architecture: str
    variant: str | None = None
    os_version: str | None = None
    os_features: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"os": self.os, "architecture": self.architecture}
        if self.variant is not None:
            result["variant"] = self.variant
        if self.os_version is not None:
            result["os.version"] = self.os_version
        if self.os_features:
            result["os.features"] = list(self.os_features)
        return result


def descriptor_digest(desc: dict[str, Any]) -> str | None:
    digest = desc.get("digest")
    return digest if isinstance(digest, str) else None


def descriptor_platform(desc: dict[str, Any]) -> dict[str, Any] | None:
    platform = desc.get("platform")
    return platform if isinstance(platform, dict) else None


def _field_rank(requested: str | None, candidate_value: str) -> int | None:
    """Return the rank of a single optional field, or None if it does not match."""
    if requested is None:
        # The caller does not constrain this field: an empty candidate value
        # is preferred, but a specific value still remains compatible.
        return _EMPTY if candidate_value == "" else _COMPATIBLE
    if candidate_value == requested:
        return _EXACT
    if candidate_value == "":
        return _EMPTY
    # Candidate specifies a different non-empty value: not compatible.
    return None


def candidate_rank(
    request: PlatformRequest, platform: dict[str, Any]
) -> tuple[int, int] | None:
    """Rank (variant_rank, os_version_rank) or None when the candidate cannot match."""

    if platform.get("os") != request.os:
        return None
    if platform.get("architecture") != request.architecture:
        return None

    variant_value = platform.get("variant")
    variant = variant_value if isinstance(variant_value, str) else ""
    variant_rank = _field_rank(request.variant, variant)
    if variant_rank is None:
        return None

    os_version_value = platform.get("os.version")
    os_version = os_version_value if isinstance(os_version_value, str) else ""
    version_rank = _field_rank(request.os_version, os_version)
    if version_rank is None:
        return None

    if request.os_features:
        features = platform.get("os.features")
        feature_set = set(features) if isinstance(features, list) else set()
        if not all(feature in feature_set for feature in request.os_features):
            return None

    # variant is ranked ahead of os.version (OCI/containerd ordering).
    return variant_rank, version_rank


def rank_label(rank: tuple[int, int]) -> dict[str, str]:
    return {
        "variant": _VARIANT_LABELS[rank[0]],
        "os.version": _VERSION_LABELS[rank[1]],
    }


def select_candidates(
    request: PlatformRequest, descriptors: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[tuple[dict[str, Any], tuple[int, int]]]]:
    """Filter and rank index child descriptors.

    Returns ``(best_descriptor, ranked)`` where ``best_descriptor`` is the
    unique best candidate (or None) and ``ranked`` contains all matching
    candidates ordered best-first.  ``best_descriptor`` is None both when
    nothing matches and when the top rank is shared.
    """

    ranked: list[tuple[dict[str, Any], tuple[int, int]]] = []
    for desc in descriptors:
        platform = descriptor_platform(desc)
        if platform is None:
            continue
        rank = candidate_rank(request, platform)
        if rank is None:
            continue
        ranked.append((desc, rank))

    ranked.sort(key=lambda item: item[1], reverse=True)
    if not ranked:
        return None, ranked

    top_rank = ranked[0][1]
    top = [item for item in ranked if item[1] == top_rank]
    if len(top) == 1:
        return top[0][0], ranked
    return None, ranked


def describe_candidate(desc: dict[str, Any], rank: tuple[int, int]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "digest": descriptor_digest(desc),
        "mediaType": desc.get("mediaType"),
        "platform": descriptor_platform(desc),
        "rank": rank_label(rank),
    }
    return {key: value for key, value in result.items() if value is not None}
