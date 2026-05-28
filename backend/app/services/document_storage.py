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
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._cache = Path(cache_dir).resolve()
        self._cache.mkdir(parents=True, exist_ok=True)

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

        # MinIO put_object
        with open(cache_path, "rb") as fp:
            self._client.put_object(
                bucket_name=self._bucket,
                object_name=object_key,
                data=fp,
                length=size,
            )

        return StoredDocument(
            object_storage_path=f"s3://{self._bucket}/{object_key}",
            local_path=str(cache_path),
            size_bytes=size,
        )

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
