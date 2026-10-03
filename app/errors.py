"""Error codes raised by the OCI manifest resolution service."""

from __future__ import annotations

from fastapi import HTTPException


class ErrorCode:
    INVALID_DIGEST = "INVALID_DIGEST"
    INVALID_DOCUMENT = "INVALID_DOCUMENT"
    MEDIA_TYPE_MISMATCH = "MEDIA_TYPE_MISMATCH"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    DIGEST_NOT_FOUND = "DIGEST_NOT_FOUND"
    NO_MATCHING_PLATFORM = "NO_MATCHING_PLATFORM"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    CIRCULAR_REFERENCE = "CIRCULAR_REFERENCE"
    DEPTH_EXCEEDED = "DEPTH_EXCEEDED"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_CHANNEL = "INVALID_CHANNEL"
    INVALID_CURSOR = "INVALID_CURSOR"
    CHANNEL_NOT_FOUND = "CHANNEL_NOT_FOUND"
    PROMOTION_FAILED = "PROMOTION_FAILED"
    PROMOTION_CONFLICT = "PROMOTION_CONFLICT"
    GC_FAILED = "GC_FAILED"


# Resolver errors are all surfaced as 409 Conflict: the stored graph itself is
# consistent, but resolving it against the given parameters fails.
ERROR_STATUS: dict[str, int] = {
    ErrorCode.INVALID_DIGEST: 400,
    ErrorCode.INVALID_DOCUMENT: 400,
    ErrorCode.MEDIA_TYPE_MISMATCH: 409,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 409,
    ErrorCode.DIGEST_NOT_FOUND: 404,
    ErrorCode.NO_MATCHING_PLATFORM: 404,
    ErrorCode.AMBIGUOUS_MATCH: 409,
    ErrorCode.CIRCULAR_REFERENCE: 409,
    ErrorCode.DEPTH_EXCEEDED: 409,
    ErrorCode.INVALID_CHANNEL: 400,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.CHANNEL_NOT_FOUND: 404,
    ErrorCode.PROMOTION_FAILED: 409,
    ErrorCode.PROMOTION_CONFLICT: 409,
    ErrorCode.GC_FAILED: 500,
}


class OCIError(HTTPException):
    """An error carrying a stable machine readable ``code``."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(status_code=ERROR_STATUS.get(code, 400), detail=message)
        self.code = code
        self.message = message
        self.details = details or {}
