"""MockAuthAdapter — *테스트 한정* 인증 우회 (ADR-018 §9).

운영 코드(`app/`)에서 본 모듈을 import하면 보안 사고다. 본 모듈은 `backend/tests/`
경로 안에서만 동작하며, conftest가 FastAPI `app.dependency_overrides`로 주입한다.

원본 위치: 이전엔 `app/core/auth_adapter.py:MockAuthAdapter` 였다 (2026-05-18 옵션 C
로 격리). yaml의 `mock.default_user` 블록도 함께 본 모듈로 흡수 — 운영 yaml에는
mock 흔적이 남지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.auth_adapter import UserContext


@dataclass(frozen=True)
class MockUserConfig:
    """테스트 default user. configs/platform/auth.yaml의 mock 블록을 코드 상수로 박음."""

    user_id: str = "dev-user-001"
    tenant_id: str = "security"
    roles: list[str] = field(default_factory=lambda: ["USER", "ADMIN"])
    clearance: str = "confidential"
    department: str | None = "security"
    domain_groups: list[str] = field(
        default_factory=lambda: ["group:security", "group:it-admin"]
    )
    preferred_username: str | None = "dev_user"
    email: str | None = "dev@example.com"


class MockAuthAdapter:
    """Bearer token 무시하고 default UserContext 반환. 테스트 한정.

    `tests/conftest.py`가 `app.dependency_overrides[get_auth_adapter] = lambda: MockAuthAdapter()`
    로 주입. path tenant_id는 그대로 mirror.
    """

    def __init__(self, default_user: MockUserConfig | None = None) -> None:
        self._default = default_user or MockUserConfig()

    async def verify_and_extract(
        self, bearer_token: str, expected_tenant_id: str
    ) -> UserContext:
        m = self._default
        return UserContext(
            user_id=m.user_id,
            tenant_id=expected_tenant_id,  # path mirror
            roles=list(m.roles),
            clearance=m.clearance,
            department=m.department,
            domain_groups=list(m.domain_groups),
            preferred_username=m.preferred_username,
            email=m.email,
        )
