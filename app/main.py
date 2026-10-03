"""FastAPI application: ingest, list and recursively resolve OCI manifests."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import channels as channels_mod
from .errors import ErrorCode, OCIError
from .oci import (
    document_kind,
    digest_bytes,
    is_manifest_media_type,
    normalize_digest,
    validate_digest,
)
from .oci import detect_media_type
from .platform import PlatformRequest
from .resolver import resolve
from .storage import (
    ChannelConflict,
    DEFAULT_DB_PATH,
    PromotionEvent,
    Repository,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_repository()
    yield


app = FastAPI(
    title="OCI Image Manifest Resolver",
    version="1.1.0",
    description=(
        "Ingest OCI Image Index / Manifest documents, resolve a unique "
        "deployable manifest for a requested platform, and promote release "
        "channels atomically."
    ),
    lifespan=lifespan,
)

_repository: Repository | None = None

# Channel names are URL-safe slugs; they must remain usable as path segments.
_CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 100


def get_repository() -> Repository:
    if _repository is None:
        init_repository()
    assert _repository is not None
    return _repository


def init_repository(db_path: str | None = None) -> Repository:
    global _repository
    _repository = Repository(db_path or DEFAULT_DB_PATH)
    return _repository


def _error_body(code: str, message: str, details: dict | None = None) -> dict:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


@app.exception_handler(OCIError)
async def oci_error_handler(_request: Request, exc: OCIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, exc.details),
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_body(
            ErrorCode.INVALID_REQUEST,
            "Request payload failed validation.",
            {"errors": exc.errors()},
        ),
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.put("/manifests/{digest:path}")
async def ingest_manifest(
    digest: str,
    http_request: Request,
    repo: Repository = Depends(get_repository),
) -> dict:
    """Ingest a raw OCI Image Index or Image Manifest under its digest."""
    digest = normalize_digest(digest)
    raw = await http_request.body()
    if not raw:
        raise OCIError(
            ErrorCode.INVALID_DOCUMENT,
            "Request body must contain the manifest JSON document.",
        )
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OCIError(
            ErrorCode.INVALID_DOCUMENT, f"Body is not valid JSON: {exc}"
        )
    if not isinstance(doc, dict):
        raise OCIError(
            ErrorCode.INVALID_DOCUMENT,
            "The manifest document must be a JSON object.",
        )

    actual_digest = digest_bytes(raw)
    if actual_digest != digest:
        raise OCIError(
            ErrorCode.INVALID_DIGEST,
            "Body digest does not match the digest in the URL.",
            {
                "requested_digest": digest,
                "actual_digest": actual_digest,
            },
        )

    media_type = detect_media_type(doc)
    created = repo.upsert(digest, media_type, doc, len(raw))
    return {
        "digest": digest,
        "mediaType": media_type,
        "kind": document_kind(media_type),
        "size": len(raw),
        "stored": True,
        "created": created,
    }


def _stored_summary(stored) -> dict:
    return {
        "digest": stored.digest,
        "mediaType": stored.media_type,
        "kind": document_kind(stored.media_type),
        "size": stored.size,
        "createdAt": stored.created_at,
    }


@app.get("/manifests")
def list_manifests(repo: Repository = Depends(get_repository)) -> dict:
    documents = repo.list_all()
    return {
        "count": len(documents),
        "documents": [_stored_summary(stored) for stored in documents],
    }


@app.get("/manifests/{digest:path}")
def get_manifest(
    digest: str, repo: Repository = Depends(get_repository)
) -> JSONResponse:
    digest = normalize_digest(digest)
    stored = repo.get(digest)
    if stored is None:
        raise OCIError(
            ErrorCode.DIGEST_NOT_FOUND,
            f"No document ingested for digest: {digest}",
            {"digest": digest},
        )
    return JSONResponse(
        content=stored.doc,
        headers={
            "Docker-Content-Digest": stored.digest,
            "Content-Type": stored.media_type,
        },
    )


@app.head("/manifests/{digest:path}")
def head_manifest(
    digest: str, repo: Repository = Depends(get_repository)
) -> Response:
    digest = normalize_digest(digest)
    stored = repo.get(digest)
    if stored is None:
        raise OCIError(
            ErrorCode.DIGEST_NOT_FOUND,
            f"No document ingested for digest: {digest}",
            {"digest": digest},
        )
    return Response(
        status_code=200,
        headers={
            "Docker-Content-Digest": stored.digest,
            "Content-Type": stored.media_type,
            "Content-Length": str(stored.size),
        },
    )


@app.post("/resolve")
def resolve_manifest(
    payload: dict[str, Any], repo: Repository = Depends(get_repository)
) -> dict:
    """Resolve a unique deployable manifest for the requested platform."""
    if not isinstance(payload, dict):
        raise OCIError(
            ErrorCode.INVALID_REQUEST, "Request body must be a JSON object."
        )

    root = payload.get("rootDigest") or payload.get("digest")
    if not isinstance(root, str) or not root:
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            "Field 'rootDigest' is required and must be a string digest.",
        )

    platform = payload.get("platform")
    if platform is None:
        platform = payload  # allow platform fields at the top level too
    request = _parse_platform(platform)
    result = resolve(repo, validate_digest(normalize_digest(root)), request)

    response = {
        "digest": result.digest,
        "mediaType": result.media_type,
        "kind": document_kind(result.media_type),
        "manifest": result.manifest,
        "descriptor": result.descriptor,
        "platform": result.platform,
        "chain": result.chain,
        "indexHops": result.index_hops,
        "request": request.to_dict(),
    }
    if not is_manifest_media_type(result.media_type):  # defensive, never happens
        response["kind"] = "unknown"
    return response


# ---------------------------------------------------------------------------
# Release channels
# ---------------------------------------------------------------------------


def _parse_platform(platform: Any) -> PlatformRequest:
    """Validate the shared platform selector used by /resolve and /promote."""
    if not isinstance(platform, dict):
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            "Field 'platform' must be an object.",
        )

    os_name = platform.get("os")
    arch = platform.get("architecture")
    if not isinstance(os_name, str) or not os_name:
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            "Platform 'os' is required.",
        )
    if not isinstance(arch, str) or not arch:
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            "Platform 'architecture' is required.",
        )

    variant = platform.get("variant")
    if variant is not None and not isinstance(variant, str):
        raise OCIError(
            ErrorCode.INVALID_REQUEST, "Platform 'variant' must be a string."
        )
    os_version = platform.get("os.version")
    if os_version is not None and not isinstance(os_version, str):
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            "Platform 'os.version' must be a string.",
        )
    os_features = platform.get("os.features") or ()
    if os_features:
        if not isinstance(os_features, list) or not all(
            isinstance(item, str) for item in os_features
        ):
            raise OCIError(
                ErrorCode.INVALID_REQUEST,
                "Platform 'os.features' must be a list of strings.",
            )

    return PlatformRequest(
        os=os_name,
        architecture=arch,
        variant=variant,
        os_version=os_version,
        os_features=tuple(os_features),
    )


def _validate_channel_name(name: str) -> str:
    if not isinstance(name, str) or not _CHANNEL_NAME_RE.match(name):
        raise OCIError(
            ErrorCode.INVALID_CHANNEL,
            "Channel name must be 1-64 characters from [A-Za-z0-9._-] and "
            "start with a letter or digit.",
            {"name": name},
        )
    return name


def _event_to_json(event: PromotionEvent) -> dict[str, Any]:
    return {
        "sequence": event.revision,
        "rootDigest": event.root_digest,
        "oldDigest": event.old_digest,
        "newDigest": event.new_digest,
        "platforms": event.platforms,
        "createdAt": event.created_at,
    }


@app.post("/channels/{name}/promote")
def promote_channel(
    name: str,
    payload: dict[str, Any],
    repo: Repository = Depends(get_repository),
) -> dict:
    """Promote a channel after every requested platform resolves uniquely.

    Platform resolutions run first; any failure aborts the promotion with
    per-platform diagnostics and leaves the channel unchanged.  When all
    platforms resolve, the channel pointer is moved in a single
    compare-and-swap transaction (``expectedDigest`` guard).
    """
    name = _validate_channel_name(name)

    if not isinstance(payload, dict):
        raise OCIError(
            ErrorCode.INVALID_REQUEST, "Request body must be a JSON object."
        )

    root = payload.get("rootDigest") or payload.get("digest")
    if not isinstance(root, str) or not root:
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            "Field 'rootDigest' is required and must be a string digest.",
        )
    root_digest = validate_digest(normalize_digest(root))

    platforms = payload.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            "Field 'platforms' is required and must be a non-empty array "
            "of platform selectors.",
        )
    if len(platforms) > channels_mod.MAX_PLATFORMS:
        raise OCIError(
            ErrorCode.INVALID_REQUEST,
            f"A promotion accepts between 1 and {channels_mod.MAX_PLATFORMS} "
            f"target platforms, got {len(platforms)}.",
            {"max_platforms": channels_mod.MAX_PLATFORMS},
        )

    targets = [_parse_platform(platform) for platform in platforms]

    expected = payload.get("expectedDigest")
    if expected is not None:
        if not isinstance(expected, str) or not expected:
            raise OCIError(
                ErrorCode.INVALID_REQUEST,
                "Field 'expectedDigest' must be a string digest when present.",
            )
        expected = validate_digest(normalize_digest(expected))

    # Phase 1: resolve every platform.  No channel state is touched here.
    successes, failures = channels_mod.plan_promotion(
        repo, root_digest, targets
    )
    if failures:
        raise OCIError(
            ErrorCode.PROMOTION_FAILED,
            f"{len(failures)} of {len(targets)} target platform(s) could not "
            "be resolved uniquely; the channel was not modified.",
            {
                "channel": name,
                "rootDigest": root_digest,
                "results": successes + failures,
                "failures": failures,
            },
        )

    # Phase 2: atomic compare-and-swap plus append to the immutable ledger.
    try:
        event = repo.promote_channel(
            name=name,
            root_digest=root_digest,
            new_digest=root_digest,
            platforms=successes,
            expected_digest=expected,
        )
    except ChannelConflict as conflict:
        raise OCIError(
            ErrorCode.PROMOTION_CONFLICT,
            "expectedDigest does not match the current channel value; the "
            "promotion was not applied.",
            {
                "channel": name,
                "expectedDigest": conflict.expected,
                "currentDigest": conflict.actual,
            },
        )

    return {
        "channel": name,
        "sequence": event.revision,
        "rootDigest": event.root_digest,
        "oldDigest": event.old_digest,
        "newDigest": event.new_digest,
        "results": successes,
        "createdAt": event.created_at,
    }


@app.get("/channels")
def list_channels(repo: Repository = Depends(get_repository)) -> dict:
    channels = repo.list_channels()
    return {
        "count": len(channels),
        "channels": [
            {
                "name": channel.name,
                "currentDigest": channel.current_digest,
                "sequence": channel.revision,
                "updatedAt": channel.updated_at,
            }
            for channel in channels
        ],
    }


@app.get("/channels/{name}")
def get_channel(name: str, repo: Repository = Depends(get_repository)) -> dict:
    name = _validate_channel_name(name)
    channel = repo.get_channel(name)
    if channel is None:
        raise OCIError(
            ErrorCode.CHANNEL_NOT_FOUND,
            f"No release channel named: {name}",
            {"name": name},
        )
    return {
        "name": channel.name,
        "currentDigest": channel.current_digest,
        "sequence": channel.revision,
        "updatedAt": channel.updated_at,
    }


@app.get("/channels/{name}/events")
def list_channel_events(
    name: str,
    limit: int = Query(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    cursor: str | None = None,
    repo: Repository = Depends(get_repository),
) -> dict:
    """Walk the immutable promotion ledger, newest sequence first."""
    name = _validate_channel_name(name)

    before_revision: int | None = None
    if cursor is not None:
        cursor_channel, before_revision = channels_mod.decode_cursor(cursor)
        if cursor_channel != name:
            raise OCIError(
                ErrorCode.INVALID_CURSOR,
                "The cursor belongs to a different channel.",
                {"cursor_channel": cursor_channel, "channel": name},
            )

    # A channel row and its first ledger entry are created together in the
    # same transaction, so a missing channel means an empty/nonexistent
    # history.
    if repo.get_channel(name) is None:
        raise OCIError(
            ErrorCode.CHANNEL_NOT_FOUND,
            f"No release channel named: {name}",
            {"name": name},
        )

    events = repo.list_events(name, before_revision=before_revision, limit=limit)
    items = [_event_to_json(event) for event in events]

    # A full page might still be the last one; only offer a cursor when an
    # older row actually exists, so clients never follow into an empty page.
    next_cursor: str | None = None
    if events and repo.has_older_events(name, events[-1].revision):
        next_cursor = channels_mod.encode_cursor(name, events[-1].revision)

    return {
        "channel": name,
        "items": items,
        "nextCursor": next_cursor,
    }
