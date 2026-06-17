"""문서 원본 at-rest 암호화 (ADR-024) — SSE-KMS 정책·배선 단위 테스트.

두 계층으로 검증한다:
  1. StorageEncryptionPolicy.resolve() — minio 비의존 순수 정책 산출.
  2. MinIOStorage.save() — 정책에 따라 put_object에 sse가 전달되는지 (fake client).

SSE 객체 헤더 단언은 minio가 설치된 환경에서만(importorskip) 수행한다.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import pytest

from app.services.document_storage import (
    MinIOStorage,
    SseSpec,
    StorageEncryptionPolicy,
)


# ---------------------------------------------------------------------------
# 1. 순수 정책 — resolve()
# ---------------------------------------------------------------------------


def test_resolve_none_returns_no_sse() -> None:
    policy = StorageEncryptionPolicy(mode="none")
    assert policy.resolve("security") is None


def test_resolve_per_tenant_key_uses_domain_id() -> None:
    policy = StorageEncryptionPolicy(mode="sse_kms", kms_key_prefix="domainrag-")
    spec = policy.resolve("security")
    assert spec == SseSpec(key_id="domainrag-security", context={"tenant_id": "security"})


def test_resolve_distinct_key_per_tenant() -> None:
    policy = StorageEncryptionPolicy(mode="sse_kms")
    a = policy.resolve("security")
    b = policy.resolve("exam-engineer")
    assert a is not None and b is not None
    assert a.key_id != b.key_id  # cross-tenant 키 격리
    assert a.context != b.context


def test_resolve_single_key_mode() -> None:
    policy = StorageEncryptionPolicy(
        mode="sse_kms", per_tenant_key=False, default_key_suffix="default"
    )
    spec = policy.resolve("security")
    assert spec is not None
    assert spec.key_id == "domainrag-default"
    # 단일 키여도 tenant context는 유지 → 객체-테넌트 결속
    assert spec.context == {"tenant_id": "security"}


def test_resolve_context_disabled() -> None:
    policy = StorageEncryptionPolicy(mode="sse_kms", bind_tenant_context=False)
    spec = policy.resolve("security")
    assert spec is not None
    assert spec.context is None


def test_resolve_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        StorageEncryptionPolicy(mode="sse_s3").resolve("security")


# ---------------------------------------------------------------------------
# 2. MinIOStorage.save() 배선 — fake client로 put_object kwargs 캡처
# ---------------------------------------------------------------------------


class _FakeMinioClient:
    """put_object 호출 인자를 기록하는 최소 fake."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_object(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)


def _save(storage: MinIOStorage, domain_id: str = "security"):
    return asyncio.run(
        storage.save(
            domain_id=domain_id,
            doc_id="doc1",
            version="v1",
            filename="report.pdf",
            stream=io.BytesIO(b"hello-bytes"),
        )
    )


def test_save_mode_none_passes_sse_none(tmp_path: Path) -> None:
    client = _FakeMinioClient()
    storage = MinIOStorage(
        client=client,
        bucket="domainrag",
        cache_dir=tmp_path / "cache",
        encryption=StorageEncryptionPolicy(mode="none"),
    )
    stored = _save(storage)
    assert stored.size_bytes == len(b"hello-bytes")
    assert len(client.calls) == 1
    assert client.calls[0]["sse"] is None
    assert client.calls[0]["object_name"] == "security/doc1/v1/report.pdf"


def test_save_default_policy_is_plaintext(tmp_path: Path) -> None:
    # encryption 미주입 → 기본 none(현 동작 보존).
    client = _FakeMinioClient()
    storage = MinIOStorage(client=client, bucket="domainrag", cache_dir=tmp_path / "c")
    _save(storage)
    assert client.calls[0]["sse"] is None


def test_save_sse_kms_passes_tenant_key_and_context(tmp_path: Path) -> None:
    pytest.importorskip("minio")
    client = _FakeMinioClient()
    storage = MinIOStorage(
        client=client,
        bucket="domainrag",
        cache_dir=tmp_path / "cache",
        encryption=StorageEncryptionPolicy(mode="sse_kms"),
    )
    _save(storage, domain_id="security")

    sse = client.calls[0]["sse"]
    assert sse is not None
    headers = sse.headers()
    assert headers["X-Amz-Server-Side-Encryption"] == "aws:kms"
    assert (
        headers["X-Amz-Server-Side-Encryption-Aws-Kms-Key-Id"] == "domainrag-security"
    )
    ctx_raw = headers["X-Amz-Server-Side-Encryption-Context"]
    assert json.loads(base64.b64decode(ctx_raw)) == {"tenant_id": "security"}
