"""AuthFusionTokenClient — OAuth2 token endpoint 위임 (ADR-018 §6).

Protocol로 분리해 테스트에서 Mock 주입 가능. 운영 구현은 httpx로 AuthFusion의
`token_endpoint` (`/oauth2/token`) + `revoke_endpoint` (`/oauth2/revoke`)를 호출.

응답 필드는 RFC 6749 표준 (access_token / refresh_token / expires_in / token_type / scope).
client_secret이 필요한 confidential client는 `client_secret_keyhub_ref`를 거쳐 KeyHub에서
runtime fetch — 본 모듈은 secret을 그대로 받아 Basic Auth로 전송한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    expires_in: int
    token_type: str = "Bearer"
    scope: str | None = None
    id_token: str | None = None  # OIDC — RP-Initiated Logout id_token_hint용 (ADR-022)
    raw: dict[str, Any] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TokenResponse":
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=payload.get("refresh_token"),
            expires_in=int(payload.get("expires_in") or 0),
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=payload.get("scope"),
            id_token=payload.get("id_token"),
            raw=payload,
        )


class TokenExchangeError(Exception):
    """token endpoint가 4xx/5xx 반환. raw payload 보존해 endpoint가 응답에 활용."""

    def __init__(self, *, status_code: int, payload: Any):
        super().__init__(f"token exchange failed: {status_code}")
        self.status_code = status_code
        self.payload = payload


class AuthFusionTokenClient(Protocol):
    """token / revoke endpoint 호출 인터페이스."""

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        client_id: str,
        redirect_uri: str,
        client_secret: str | None = None,
    ) -> TokenResponse: ...

    async def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str | None = None,
    ) -> TokenResponse: ...

    async def revoke(
        self,
        *,
        token: str,
        client_id: str,
        client_secret: str | None = None,
        token_type_hint: str = "access_token",
    ) -> None: ...


class HttpxAuthFusionTokenClient:
    """운영 구현체 — httpx.AsyncClient로 token/revoke endpoint 호출.

    AuthFusion은 CONFIDENTIAL client + PKCE를 권장한다 (OIDC-INTEGRATION-GUIDE §8).
    `default_client_secret`을 생성자에 주입하면 호출 시점에 명시되지 않더라도 자동 적용.
    """

    def __init__(
        self,
        *,
        token_endpoint: str,
        revoke_endpoint: str | None = None,
        timeout_seconds: float = 5.0,
        default_client_secret: str | None = None,
    ) -> None:
        if not token_endpoint:
            raise ValueError("token_endpoint required")
        self._token_endpoint = token_endpoint
        self._revoke_endpoint = revoke_endpoint
        self._timeout = timeout_seconds
        self._default_secret = default_client_secret

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        client_id: str,
        redirect_uri: str,
        client_secret: str | None = None,
    ) -> TokenResponse:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
        return await self._post_form(form, client_id=client_id, client_secret=client_secret)

    async def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str | None = None,
    ) -> TokenResponse:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        return await self._post_form(form, client_id=client_id, client_secret=client_secret)

    async def revoke(
        self,
        *,
        token: str,
        client_id: str,
        client_secret: str | None = None,
        token_type_hint: str = "access_token",
    ) -> None:
        if not self._revoke_endpoint:
            return  # revoke endpoint 미설정 — 운영자가 끔. silent no-op.
        form = {
            "token": token,
            "client_id": client_id,
            "token_type_hint": token_type_hint,
        }
        secret = client_secret if client_secret is not None else self._default_secret
        auth = (client_id, secret) if secret else None
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._revoke_endpoint, data=form, auth=auth)
        # RFC 7009: revoke는 200 OK 정상. 4xx도 무시(이미 만료된 토큰 등).

    async def _post_form(
        self,
        form: dict[str, str],
        *,
        client_id: str,
        client_secret: str | None,
    ) -> TokenResponse:
        secret = client_secret if client_secret is not None else self._default_secret
        auth = (client_id, secret) if secret else None
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._token_endpoint, data=form, auth=auth)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                payload = {"error": "unparseable", "body": resp.text}
            raise TokenExchangeError(status_code=resp.status_code, payload=payload)
        return TokenResponse.from_payload(resp.json())
