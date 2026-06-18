"""DocumentStorage — 업로드된 원본 파일의 영속화 (ADR-008/019 prefix-per-tenant).

운영 (MinIOStorage):
  bucket/{domain_id}/{doc_id}/{version}/{filename} 으로 prefix-per-tenant 적재.
  반환되는 object_storage_path는 documents.object_storage_path 컬럼에 그대로 저장된다.

로컬 dev / 테스트 (LocalFilesystemStorage):
  base_dir/{domain_id}/{doc_id}/{version}/{filename} 로 평문 저장. parser가 file_path를
  바로 읽도록 절대 경로를 반환.

Protocol 형태로 정의해 IndexingService와 분리. caller(API endpoint)가 storage.save를
먼저 호출하고, IndexingService에는 반환된 local_path / object_storage_path를 같이 전달한다.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol


@dataclass
class StoredDocument:
    """Storage.save() 반환값."""

    object_storage_path: str  # documents.object_storage_path에 저장될 logical path
    local_path: str  # parser가 즉시 읽을 수 있는 절대 경로 (MinIO도 임시 캐시)
    size_bytes: int


# ----------------------------------------------------------------------------
# At-rest 암호화 정책 (ADR-024) — minio 비의존 순수 정책 계층.
# MinIOStorage가 put_object 마다 SSE-KMS 명세를 산출하는 데 사용한다.
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SseSpec:
    """SSE-KMS 적용 명세 — 정책 산출물(minio 객체로 번역되기 전 순수 데이터).

    key_id: KMS 키 식별자 (테넌트별 키면 `{prefix}{domain_id}`).
    context: encryption context(AAD). KMS가 복호 시 동일 값을 요구 → 객체를
        해당 tenant에 암호학적으로 바인딩. None이면 context 미부여.
    """

    key_id: str
    context: dict[str, str] | None = None


@dataclass(frozen=True)
class StorageEncryptionPolicy:
    """문서 원본 at-rest 암호화 정책 (ADR-024).

    configs/platform/storage.yaml ↔ env(MINIO_SSE_*)에서 주입된다. 코드 하드코딩
    금지 원칙(절대원칙 8)에 따라 MinIOStorage는 본 정책을 받아서만 동작한다.

    mode="none"이 기본 — 어떤 SSE든 MinIO 서버에 KMS 백엔드가 선행 구성돼야 하며,
    미구성 상태로 sse_kms를 켜면 put_object가 500을 낸다(ADR-024 §3·§5).
    """

    mode: str = "none"  # "sse_kms" | "none"
    kms_key_prefix: str = "domainrag-"
    per_tenant_key: bool = True
    bind_tenant_context: bool = True
    default_key_suffix: str = "default"

    def resolve(self, domain_id: str) -> SseSpec | None:
        """domain_id에 대한 SSE 명세 산출. mode=none이면 None(암호화 미적용)."""
        if self.mode == "none":
            return None
        if self.mode != "sse_kms":
            raise ValueError(f"unsupported MINIO_SSE_MODE: {self.mode!r}")
        if self.per_tenant_key:
            key_id = f"{self.kms_key_prefix}{domain_id}"
        else:
            key_id = f"{self.kms_key_prefix}{self.default_key_suffix}"
        context = {"tenant_id": domain_id} if self.bind_tenant_context else None
        return SseSpec(key_id=key_id, context=context)


def _object_key(object_path: str) -> str:
    """object_storage_path → 객체 key. ``s3://bucket/key`` 형식이면 bucket 부분 제거."""
    if object_path.startswith("s3://"):
        rest = object_path[len("s3://"):]
        return rest.split("/", 1)[1] if "/" in rest else rest
    return object_path


class DocumentStorage(Protocol):
    """업로드 원본 파일 저장 인터페이스."""

    async def save(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str,
        filename: str,
        stream: IO[bytes],
    ) -> StoredDocument: ...

    async def load(self, *, object_path: str) -> bytes:
        """저장된 객체를 바이트로 읽는다 (ADR-025 §4 figure-reuse용 그림 로드).

        object_path는 save가 반환한 object_storage_path (운영: ``s3://bucket/key``,
        로컬: 상대경로) 또는 그에 준하는 key. 없는 객체는 구현체별 예외를 던진다.
        """
        ...

    async def delete(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str | None = None,
    ) -> int:
        """ADR-007/012 hard delete — 해당 doc의 모든 (또는 특정 version) 원본 파일 삭제.

        Returns: 삭제된 file 수.
        """
        ...


class LocalFilesystemStorage:
    """파일 시스템 기반 storage — 로컬 dev / 테스트 전용.

    base_dir 하위에 prefix-per-tenant 디렉토리 구조로 저장. parser는 local_path를 직접
    읽는다. 운영 (MinIOStorage)와 동일한 path layout을 사용해 마이그레이션 친화적.
    """

    def __init__(self, *, base_dir: str | Path) -> None:
        self._base = Path(base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    async def save(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str,
        filename: str,
        stream: IO[bytes],
    ) -> StoredDocument:
        safe_filename = os.path.basename(filename) or "upload.bin"
        rel_dir = Path(domain_id) / doc_id / version
        target_dir = self._base / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename

        size = 0
        with open(target_path, "wb") as out:
            shutil.copyfileobj(stream, out)
            size = out.tell()

        object_storage_path = str(rel_dir / safe_filename).replace("\\", "/")
        return StoredDocument(
            object_storage_path=object_storage_path,
            local_path=str(target_path),
            size_bytes=size,
        )

    async def load(self, *, object_path: str) -> bytes:
        # 로컬은 base 기준 상대경로. 방어적으로 s3 URI도 key로 환원.
        key = _object_key(object_path)
        return (self._base / key).read_bytes()

    async def delete(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str | None = None,
    ) -> int:
        """파일 + 빈 디렉토리 cleanup. version None이면 doc_id 디렉토리 전체."""
        if version is None:
            target = self._base / domain_id / doc_id
        else:
            target = self._base / domain_id / doc_id / version
        if not target.exists():
            return 0
        count = 0
        if target.is_file():
            target.unlink()
            count = 1
        else:
            for p in target.rglob("*"):
                if p.is_file():
                    p.unlink()
                    count += 1
            shutil.rmtree(target, ignore_errors=True)
        return count


class MinIOStorage:
    """MinIO put_object 기반 storage (ADR-008/019).

    bucket/{domain_id}/{doc_id}/{version}/{filename} layout. parser는 즉시 file_path를
    필요로 하므로 업로드 후 같은 layout으로 로컬 cache_dir에도 복사해 local_path를 제공.
    """

    def __init__(
        self,
        *,
        client,
        bucket: str,
        cache_dir: str | Path,
        encryption: StorageEncryptionPolicy | None = None,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._cache = Path(cache_dir).resolve()
        self._cache.mkdir(parents=True, exist_ok=True)
        # ADR-024 — 미주입 시 mode=none(평문). 운영은 deps에서 정책을 주입한다.
        self._encryption = encryption or StorageEncryptionPolicy()

    def _build_sse(self, domain_id: str):
        """정책 → minio SseKMS 객체 번역 (지연 import). 미적용이면 None."""
        spec = self._encryption.resolve(domain_id)
        if spec is None:
            return None
        from minio.sse import SseKMS  # 지연 import — 순수 정책 계층은 minio 비의존

        return SseKMS(spec.key_id, spec.context or {})

    async def save(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str,
        filename: str,
        stream: IO[bytes],
    ) -> StoredDocument:
        safe_filename = os.path.basename(filename) or "upload.bin"
        rel = Path(domain_id) / doc_id / version / safe_filename
        object_key = str(rel).replace("\\", "/")

        # 캐시 먼저 기록 (size 확정 + parser 즉시 접근용)
        cache_path = self._cache / rel
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as out:
            shutil.copyfileobj(stream, out)
        size = cache_path.stat().st_size

        # MinIO put_object — ADR-024: 정책에 따라 SSE-KMS 부여(미적용이면 sse=None).
        sse = self._build_sse(domain_id)
        with open(cache_path, "rb") as fp:
            self._client.put_object(
                bucket_name=self._bucket,
                object_name=object_key,
                data=fp,
                length=size,
                sse=sse,
            )

        return StoredDocument(
            object_storage_path=f"s3://{self._bucket}/{object_key}",
            local_path=str(cache_path),
            size_bytes=size,
        )

    async def load(self, *, object_path: str) -> bytes:
        key = _object_key(object_path)
        resp = self._client.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    async def delete(
        self,
        *,
        domain_id: str,
        doc_id: str,
        version: str | None = None,
    ) -> int:
        """MinIO prefix 삭제 + 캐시 cleanup. version None이면 doc_id prefix 전체."""
        prefix_parts = [domain_id, doc_id]
        if version is not None:
            prefix_parts.append(version)
        prefix = "/".join(prefix_parts) + "/"

        # list_objects + remove_objects
        objects = list(
            self._client.list_objects(
                bucket_name=self._bucket, prefix=prefix, recursive=True
            )
        )
        count = 0
        for obj in objects:
            self._client.remove_object(
                bucket_name=self._bucket, object_name=obj.object_name
            )
            count += 1

        # 캐시도 cleanup (parser가 다시 안 읽도록)
        if version is None:
            cache_target = self._cache / domain_id / doc_id
        else:
            cache_target = self._cache / domain_id / doc_id / version
        if cache_target.exists():
            shutil.rmtree(cache_target, ignore_errors=True)

        return count
