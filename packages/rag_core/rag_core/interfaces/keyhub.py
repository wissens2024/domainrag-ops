"""KeyHubAdapter Protocol — LoRA·secret 보관 (ADR-019)."""

from __future__ import annotations

from typing import Protocol


class KeyHubAdapter(Protocol):
    """AuthFusion KeyHub (port 8085) 또는 LocalSecretStore.

    구현체:
      - AuthFusionKeyHub (운영)
      - LocalSecretStore (dev fallback)
    """

    async def put_secret(self, key: str, value: bytes, *, metadata: dict | None = None) -> str:
        """secret 저장. 반환: KeyHub key id (또는 ref URI)."""
        ...

    async def get_secret(self, key: str) -> bytes:
        """envelope 복호화 후 평문 반환."""
        ...

    async def delete_secret(self, key: str) -> None: ...

    async def list_secrets(self, prefix: str) -> list[str]: ...
