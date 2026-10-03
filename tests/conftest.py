import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.storage import Repository


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest(config_digest: str = "sha256:" + "0" * 64,
             media_type: str = "application/vnd.oci.image.manifest.v1+json") -> tuple[str, bytes, dict]:
    doc = {
        "schemaVersion": 2,
        "mediaType": media_type,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": 1234,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + "1" * 64,
                "size": 5678,
            }
        ],
    }
    raw = json.dumps(doc, separators=(",", ":")).encode()
    return sha256_digest(raw), raw, doc


def index(children: list[dict],
          media_type: str = "application/vnd.oci.image.index.v1+json") -> tuple[str, bytes, dict]:
    doc = {"schemaVersion": 2, "mediaType": media_type, "manifests": children}
    raw = json.dumps(doc, separators=(",", ":")).encode()
    return sha256_digest(raw), raw, doc


def child(digest: str, os_name: str, arch: str, *,
          variant: str | None = None, os_version: str | None = None,
          os_features: list[str] | None = None,
          media_type: str = "application/vnd.oci.image.manifest.v1+json",
          size: int = 100) -> dict:
    platform = {"os": os_name, "architecture": arch}
    if variant is not None:
        platform["variant"] = variant
    if os_version is not None:
        platform["os.version"] = os_version
    if os_features is not None:
        platform["os.features"] = os_features
    return {
        "mediaType": media_type,
        "digest": digest,
        "size": size,
        "platform": platform,
    }


@pytest.fixture
def repo(tmp_path):
    return Repository(str(tmp_path / "test.db"))


@pytest.fixture
def client(repo):
    main_module._repository = repo
    test_client = TestClient(main_module.app, raise_server_exceptions=True)
    yield test_client
    main_module._repository = None


def ingest(client: TestClient, raw: bytes, digest: str | None = None) -> str:
    digest = digest or sha256_digest(raw)
    response = client.put(f"/manifests/{digest}", content=raw,
                          headers={"Content-Type": "application/vnd.oci.image.index.v1+json"})
    assert response.status_code == 200, response.text
    return digest
