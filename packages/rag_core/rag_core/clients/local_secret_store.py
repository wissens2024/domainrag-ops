"""LocalSecretStore — KeyHubAdapter 파일시스템 구현체 (ADR-019 §8 dev fallback).

운영(폐쇄망)은 `AuthFusionKeyHub` (port 8085) 사용. 본 구현체는 dev/test에서
파일시스템에 secret blob + sidecar metadata를 저장한다. 동일 Protocol을 만족하므로
운영 교체 시 코드 변경 0.

저장 구조::

    <base_path>/
      ├─ <safe_key>.bin       # secret blob (선택적 Fernet 암호화)
      └─ <safe_key>.meta.json # {created_at, encrypted: bool, original_key, metadata}

key 정규화:
  - `/`, `\\`, `..` 같은 path traversal 문자를 `_`로 치환
  - URL-unsafe 문자는 그대로 두되 OS 파일명 한계는 caller가 책임

Fernet master key:
  - 생성자 인자 `fernet_key`로 주입. None이면 평문 저장(개발 모드만 권장).
  - 운영 dev 환경은 `KEYHUB_LOCAL_FERNET_KEY` env로 주입.

`ADR-019 §8` 진실 소스: 실 운영은 AuthFusionKeyHub. LocalSecretStore는 backend
docker-compose 단독 실행·integration test 격리 환경 등을 위한 dev fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class SecretNotFoundError(KeyError):
    """get_secret에서 key가 없을 때."""


@dataclass
class SecretMetadata:
    created_at: str
    encrypted: bool
    original_key: str
    metadata: dict[str, Any]


class LocalSecretStore:
    """파일시스템 기반 KeyHubAdapter 구현.

    Args:
        base_path: secret 저장 디렉터리. 없으면 자동 생성.
        fernet_key: 32-byte url-safe base64 Fernet key. None이면 평문 저장.
        ref_prefix: 반환되는 secret reference의 prefix (운영 환경 호환).
    """

    def __init__(
        self,
        *,
        base_path: Path,
        fernet_key: bytes | None = None,
        ref_prefix: str = "local",
    ) -> None:
        self._base = Path(base_path)
        self._base.mkdir(parents=True, exist_ok=True)
        self._fernet = None
        if fernet_key:
            try:
                from cryptography.fernet import Fernet  # type: ignore[import-not-found]

                self._fernet = Fernet(fernet_key)
            except ImportError:
                logger.warning(
                    "cryptography 미설치 — LocalSecretStore가 평문 저장으로 fallback"
                )
                self._fernet = None
            except Exception as exc:  # noqa: BLE001
                logger.warning("fernet_key 무효 — 평문 저장으로 fallback: %s", exc)
                self._fernet = None
        self._ref_prefix = ref_prefix

    @staticmethod
    def _safe_key(key: str) -> str:
        if not key:
            raise ValueError("empty_key")
        # path traversal 방지 — '/' / '\\' / '..' 모두 제거
        s = key.replace("/", "_").replace("\\", "_").replace("..", "_")
        s = _UNSAFE_CHARS.sub("_", s)
        return s

    def _blob_path(self, key: str) -> Path:
        return self._base / f"{self._safe_key(key)}.bin"

    def _meta_path(self, key: str) -> Path:
        return self._base / f"{self._safe_key(key)}.meta.json"

    async def put_secret(
        self, key: str, value: bytes, *, metadata: dict | None = None
    ) -> str:
        """secret 저장. 반환: ref URI (e.g., "local://<safe_key>")."""
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("value must be bytes")
        encrypted_flag = False
        payload = bytes(value)
        if self._fernet is not None:
            payload = self._fernet.encrypt(payload)
            encrypted_flag = True
        blob_path = self._blob_path(key)
        meta_path = self._meta_path(key)
        # write blob atomically (tmp + rename)
        tmp_blob = blob_path.with_suffix(blob_path.suffix + ".tmp")
        tmp_blob.write_bytes(payload)
        tmp_blob.replace(blob_path)
        meta_obj = SecretMetadata(
            created_at=datetime.now(timezone.utc).isoformat(),
            encrypted=encrypted_flag,
            original_key=key,
            metadata=dict(metadata or {}),
        )
        meta_path.write_text(
            json.dumps(
                {
                    "created_at": meta_obj.created_at,
                    "encrypted": meta_obj.encrypted,
                    "original_key": meta_obj.original_key,
                    "metadata": meta_obj.metadata,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return f"{self._ref_prefix}://{self._safe_key(key)}"

    async def get_secret(self, key: str) -> bytes:
        """ref 또는 raw key 둘 다 받음."""
        raw = self._strip_ref_prefix(key)
        blob_path = self._blob_path(raw)
        meta_path = self._meta_path(raw)
        if not blob_path.exists():
            raise SecretNotFoundError(key)
        encrypted = False
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                encrypted = bool(meta.get("encrypted"))
            except Exception:  # noqa: BLE001
                encrypted = False
        blob = blob_path.read_bytes()
        if encrypted and self._fernet is not None:
            return self._fernet.decrypt(blob)
        return blob

    async def delete_secret(self, key: str) -> None:
        raw = self._strip_ref_prefix(key)
        for p in (self._blob_path(raw), self._meta_path(raw)):
            try:
                p.unlink(missing_ok=True)
            except FileNotFoundError:
                pass

    async def list_secrets(self, prefix: str) -> list[str]:
        """prefix로 시작하는 original_key 들의 목록 반환 (정렬됨)."""
        result: list[str] = []
        for meta_path in self._base.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            original = meta.get("original_key")
            if isinstance(original, str) and original.startswith(prefix):
                result.append(original)
        result.sort()
        return result

    def _strip_ref_prefix(self, key: str) -> str:
        full_prefix = f"{self._ref_prefix}://"
        if key.startswith(full_prefix):
            return key[len(full_prefix):]
        return key
