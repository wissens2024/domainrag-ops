"""AuthAdapter — JWT 검증 + UserContext 빌드 (ADR-008 §7, ADR-018)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class UserContext:
    """ADR-018 §5."""

    user_id: str
    tenant_id: str
    roles: list[str] = field(default_factory=list)
    clearance: str = "internal"
    department: str | None = None
    domain_groups: list[str] = field(default_factory=list)
    preferred_username: str | None = None
    email: str | None = None
    raw_claims: dict = field(default_factory=dict)

    @property
    def is_platform_admin(self) -> bool:
        return "PLATFORM_ADMIN" in self.roles


class AuthAdapter(Protocol):
    """ADR-008 §7 Protocol stub의 구체 인터페이스."""

    async def verify_and_extract(
        self, bearer_token: str, expected_tenant_id: str
    ) -> UserContext: ...
