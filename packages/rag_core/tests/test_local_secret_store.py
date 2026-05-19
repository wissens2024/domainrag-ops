"""LocalSecretStore — KeyHub dev fallback 검증 (ADR-019 §8)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rag_core.clients.local_secret_store import (
    LocalSecretStore,
    SecretNotFoundError,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_put_get_roundtrip_plaintext(tmp_path: Path):
    store = LocalSecretStore(base_path=tmp_path)
    ref = _run(store.put_secret("k1", b"hello"))
    assert ref == "local://k1"
    blob = _run(store.get_secret(ref))
    assert blob == b"hello"


def test_get_by_raw_key(tmp_path: Path):
    store = LocalSecretStore(base_path=tmp_path)
    _run(store.put_secret("k1", b"hello"))
    assert _run(store.get_secret("k1")) == b"hello"


def test_path_traversal_normalized(tmp_path: Path):
    store = LocalSecretStore(base_path=tmp_path)
    ref = _run(store.put_secret("lora/security/a", b"x"))
    # 'lora/security/a' → 'lora_security_a' (safe)
    assert ref == "local://lora_security_a"
    blob = _run(store.get_secret("lora/security/a"))
    assert blob == b"x"


def test_metadata_persisted(tmp_path: Path):
    store = LocalSecretStore(base_path=tmp_path)
    _run(store.put_secret("k1", b"x", metadata={"tenant": "security"}))
    import json

    meta = json.loads((tmp_path / "k1.meta.json").read_text(encoding="utf-8"))
    assert meta["original_key"] == "k1"
    assert meta["metadata"]["tenant"] == "security"


def test_delete_idempotent(tmp_path: Path):
    store = LocalSecretStore(base_path=tmp_path)
    _run(store.put_secret("k1", b"x"))
    _run(store.delete_secret("k1"))
    _run(store.delete_secret("k1"))  # 이미 삭제됨 — silent
    with pytest.raises(SecretNotFoundError):
        _run(store.get_secret("k1"))


def test_list_secrets_prefix(tmp_path: Path):
    store = LocalSecretStore(base_path=tmp_path)
    _run(store.put_secret("lora/security/a", b"x"))
    _run(store.put_secret("lora/legal/b", b"x"))
    _run(store.put_secret("other/c", b"x"))
    items = _run(store.list_secrets("lora/"))
    assert items == ["lora/legal/b", "lora/security/a"]


def test_empty_key_rejected(tmp_path: Path):
    store = LocalSecretStore(base_path=tmp_path)
    with pytest.raises(ValueError):
        _run(store.put_secret("", b"x"))


def test_fernet_roundtrip(tmp_path: Path):
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        pytest.skip("cryptography not installed")

    key = Fernet.generate_key()
    store = LocalSecretStore(base_path=tmp_path, fernet_key=key)
    _run(store.put_secret("k1", b"hello"))
    # 디스크에 저장된 blob은 평문이 아님
    blob_on_disk = (tmp_path / "k1.bin").read_bytes()
    assert blob_on_disk != b"hello"
    # get_secret는 복호화
    assert _run(store.get_secret("k1")) == b"hello"
