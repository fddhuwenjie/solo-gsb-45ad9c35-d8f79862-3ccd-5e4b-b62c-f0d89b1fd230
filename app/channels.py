"""Release channel promotion orchestration and history cursors.

A promotion resolves a unique deployable manifest for every requested
platform *before* touching the channel pointer.  When any platform fails the
channel is left untouched and every per-platform diagnostic is returned in a
single response.  Only when all resolutions succeed is the pointer moved in
one compare-and-swap SQLite transaction.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import ErrorCode, OCIError
from .platform import PlatformRequest
from .resolver import resolve

MAX_PLATFORMS = 32


def _platform_result(result) -> dict[str, Any]:
    """Serialize one successful platform resolution for the promotion ledger."""
    return {
        "request": None,
        "platform": result.platform,
        "digest": result.digest,
        "mediaType": result.media_type,
        "descriptor": result.descriptor,
        "chain": result.chain,
        "indexHops": result.index_hops,
    }


def _error_entry(request: PlatformRequest, exc: OCIError) -> dict[str, Any]:
    return {
        "request": request.to_dict(),
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    }


def plan_promotion(
    repo, root_digest: str, targets: list[PlatformRequest]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve every target platform against ``root_digest``.

    Returns ``(successes, failures)`` where each success is a ledger-ready
    result record (also carrying the originating request) and each failure
    is a platform-level diagnostic.  All targets are attempted even after an
    earlier one fails, so a rejected promotion reports every problem at
    once.
    """
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for target in targets:
        try:
            result = resolve(repo, root_digest, target)
        except OCIError as exc:
            failures.append(_error_entry(target, exc))
            continue
        entry = _platform_result(result)
        entry["request"] = target.to_dict()
        successes.append(entry)
    return successes, failures


def encode_cursor(channel: str, revision: int) -> str:
    """Build an opaque history cursor pointing just before ``revision``."""
    raw = json.dumps(
        {"channel": channel, "before": revision}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int]:
    """Inverse of :func:`encode_cursor`; raises INVALID_CURSOR on bad input."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
        channel = payload["channel"]
        revision = payload["before"]
    except Exception as exc:  # malformed base64 / JSON / missing fields
        raise OCIError(
            ErrorCode.INVALID_CURSOR,
            f"The supplied cursor is not valid: {exc}",
            {"cursor": cursor},
        )
    if not isinstance(channel, str) or not isinstance(revision, int) \
            or isinstance(revision, bool) or revision < 1:
        raise OCIError(
            ErrorCode.INVALID_CURSOR,
            "The supplied cursor does not encode a valid channel position.",
            {"cursor": cursor},
        )
    return channel, revision
