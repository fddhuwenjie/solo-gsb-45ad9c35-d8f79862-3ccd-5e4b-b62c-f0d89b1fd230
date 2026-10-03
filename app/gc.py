"""Mark-and-sweep reachability for manifest garbage collection.

The sweep roots are the current digest of every release channel plus the
``rootDigest`` of every immutable promotion ledger entry: anything a
release record can still resolve must survive.  From those roots the
``manifests`` arrays of Image Index documents are followed recursively;
every stored document outside the reachable set is garbage.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .oci import child_manifests, is_index_media_type


def reachable_digests(
    documents: Mapping[str, tuple[str, dict[str, Any]]],
    roots: Iterable[str],
) -> set[str]:
    """Return the stored digests reachable from ``roots``.

    ``documents`` maps each stored digest to ``(media_type, doc)``.  Only
    Index documents have children to follow; a digest that is referenced
    but not stored simply marks nothing (dangling references are the
    resolver's concern, not the sweep's).
    """
    reachable: set[str] = set()
    stack = [digest for digest in dict.fromkeys(roots) if digest in documents]
    while stack:
        digest = stack.pop()
        if digest in reachable or digest not in documents:
            continue
        reachable.add(digest)
        media_type, doc = documents[digest]
        if not is_index_media_type(media_type):
            continue
        for descriptor in child_manifests(doc):
            if not isinstance(descriptor, dict):
                continue
            child = descriptor.get("digest")
            if isinstance(child, str) and child not in reachable:
                stack.append(child)
    return reachable


def select_garbage(
    documents: Mapping[str, Any],
    reachable: set[str],
    limit: int | None,
) -> tuple[list[str], int]:
    """Choose garbage digests to delete, smallest digest first.

    Returns ``(selected, remaining)``: up to ``limit`` lexicographically
    smallest unreachable digests (all of them when ``limit`` is None)
    and how many unreachable digests are left over.
    """
    garbage = sorted(digest for digest in documents if digest not in reachable)
    selected = garbage if limit is None else garbage[:limit]
    return selected, len(garbage) - len(selected)
