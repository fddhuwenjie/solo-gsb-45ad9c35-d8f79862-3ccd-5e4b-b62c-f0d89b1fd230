"""OCI image manifest / index constants and document helpers.

Media types cover both the current OCI types and the legacy Docker v2 schema
2 types, which share the same JSON structure.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .errors import ErrorCode, OCIError

OCI_IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_IMAGE_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI_EMPTY_JSON = "application/vnd.oci.empty.v1+json"
OCI_IMAGE_CONFIG = "application/vnd.oci.image.config.v1+json"
DOCKER_IMAGE_CONFIG = "application/vnd.docker.container.image.v1+json"

INDEX_MEDIA_TYPES = frozenset({OCI_IMAGE_INDEX, DOCKER_MANIFEST_LIST})
MANIFEST_MEDIA_TYPES = frozenset({OCI_IMAGE_MANIFEST, DOCKER_IMAGE_MANIFEST})
KNOWN_DOCUMENT_MEDIA_TYPES = INDEX_MEDIA_TYPES | MANIFEST_MEDIA_TYPES

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_digest(digest: str) -> str:
    """Validate a ``sha256:<hex>`` digest, returning it unchanged."""
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise OCIError(
            ErrorCode.INVALID_DIGEST,
            f"Digest must be of the form sha256:<64 lowercase hex chars>: {digest!r}",
        )
    return digest


def normalize_digest(value: str) -> str:
    """Accept either ``sha256:<hex>`` or a bare 64 char hex string."""
    if isinstance(value, str) and _HEX64_RE.match(value):
        return "sha256:" + value
    return validate_digest(value)


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def is_index_media_type(media_type: str | None) -> bool:
    return media_type in INDEX_MEDIA_TYPES


def is_manifest_media_type(media_type: str | None) -> bool:
    return media_type in MANIFEST_MEDIA_TYPES


def document_kind(media_type: str | None) -> str:
    if is_index_media_type(media_type):
        return "index"
    if is_manifest_media_type(media_type):
        return "manifest"
    return "unknown"


def detect_media_type(doc: dict[str, Any]) -> str:
    """Determine the document type.

    An explicit ``mediaType`` field wins (as produced by older tools) but
    must agree with the JSON shape (``manifests`` vs ``config``);
    otherwise the shape decides.
    """

    explicit = doc.get("mediaType")
    if isinstance(explicit, str) and explicit in KNOWN_DOCUMENT_MEDIA_TYPES:
        shape = None
        if isinstance(doc.get("manifests"), list):
            shape = "index"
        elif isinstance(doc.get("config"), dict):
            shape = "manifest"
        if shape is not None and document_kind(explicit) != shape:
            raise OCIError(
                ErrorCode.INVALID_DOCUMENT,
                f"mediaType {explicit} does not match the document shape "
                f"(detected {shape}).",
                {"mediaType": explicit, "shape": shape},
            )
        return explicit
    if isinstance(explicit, str) and explicit:
        # An explicit but unknown media type cannot be trusted / resolved.
        raise OCIError(
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported document mediaType: {explicit}",
            {"mediaType": explicit},
        )
    if isinstance(doc.get("manifests"), list):
        return OCI_IMAGE_INDEX
    if isinstance(doc.get("config"), dict):
        return OCI_IMAGE_MANIFEST
    raise OCIError(
        ErrorCode.INVALID_DOCUMENT,
        "Document is neither an image index nor an image manifest: "
        "expected a 'manifests' array (index) or a 'config' object (manifest).",
    )


def child_manifests(index_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return child descriptors of an index, preserving document order."""
    children = index_doc.get("manifests")
    return children if isinstance(children, list) else []
