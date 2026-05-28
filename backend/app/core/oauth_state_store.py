"""OAuth2 PKCE state store — `/authorize` → `/callback` 사이의 임시 상태 보관소 (ADR-018 §2).

state는 CSRF 토큰 겸 lookup key. `put`으로 등록한 항목은 단 한 번 `pop`되며 만료된 항목은
자동 제거된다 (replay 방지).

Protocol로 분리 — 운영 다중-인스턴스 환경에서는 Redis 구현체로 교체. 본 모듈은 InMemory
구현(단일 프로세스 dev/test 기본)을 제공.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OAuthStateEntry:
    state: str
    domain_id: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    expires_at: float  # epoch seconds


class OAuthStateStore(Protocol):
    def put(self, entry: OAuthStateEntry) -> None: ...
    def pop(self, state: str) -> OAuthStateEntry | None: ...


class InMemoryOAuthStateStore:
    """단일 프로세스 dev/test용. 멀티 인스턴스 운영에는 RedisOAuthStateStore 필요."""

    def __init__(self, *, clock=time.time) -> None:
        self._entries: dict[str, OAuthStateEntry] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def put(self, entry: OAuthStateEntry) -> None:
        with self._lock:
            self._gc()
            self._entries[entry.state] = entry

    def pop(self, state: str) -> OAuthStateEntry | None:
        with self._lock:
            self._gc()
            entry = self._entries.pop(state, None)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                return None  # 이미 만료 — pop 자체는 했으니 replay 차단
            return entry

    def _gc(self) -> None:
        now = self._clock()
        expired = [s for s, e in self._entries.items() if e.expires_at <= now]
        for s in expired:
            self._entries.pop(s, None)


def generate_state() -> str:
    """CSRF state — 256bit URL-safe."""
    return secrets.token_urlsafe(32)


def generate_pkce_pair() -> tuple[str, str]:
    """RFC 7636 — (code_verifier, code_challenge S256). caller는 challenge만 노출."""
    import base64
    import hashlib

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge
