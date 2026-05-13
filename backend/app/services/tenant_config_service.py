"""TenantConfigOverrideService — ADR-009 §3·§8 tenant_config_overrides CRUD + audit + ledger.

흐름 (PATCH):
  1. restricted_to 검증 — 키가 platform_admin 전용이면 일반 admin reject(403)
  2. 기존 value 조회 (old_value)
  3. UPSERT into tenant_config_overrides
  4. INSERT into tenant_config_change_logs (old/new/who/when/reason)
  5. config_change_breaking 이면 Ledger publish_config_change_breaking
  6. TenantConfigService.invalidate(tenant_id) → 다음 chat 요청에 즉시 반영

restricted_to / breaking 키 정의:
  본 service에 명시적 set으로 보관. ADR-009 §8 권고에 따라 model.tenant_slm.endpoint 등 운영
  영향이 큰 키를 platform_admin 전용으로 둔다. breaking 키는 그 외에도 retrieval.dense_dim
  같은 schema 깨지는 키 포함.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.rls import set_tenant_context
from app.core.tenant_config_service import TenantConfigService

logger = logging.getLogger(__name__)


# ADR-009 §8 — platform_admin 전용으로 제한할 키 (category.path 형식).
# 운영자가 잘못 건드리면 전체 tenant chat이 중단되므로 별도 RBAC 분리.
PLATFORM_ADMIN_RESTRICTED_KEYS: frozenset[str] = frozenset(
    {
        "model.endpoints.tenant_slm.base_url",
        "model.endpoints.shared_llm.base_url",
        "model.endpoints.embedder.base_url",
        "model.endpoints.reranker.base_url",
        "model.endpoints.ollama_vision.base_url",
        "model.endpoints.tenant_slm.tensor_parallel_size",
        "model.endpoints.shared_llm.tensor_parallel_size",
        "retrieval.embedding.model",           # 모델 교체 = ADR-019 §5 마이그레이션 워크플로우
        "retrieval.dense_dim",                  # collection 깨짐
        "auth.authfusion.issuer",               # 인증 깨짐
        "auth.authfusion.jwks_uri",
        "lifecycle.archival.chunks_archive_days",   # 운영 정책 변경
        "lifecycle.deletion.default",
    }
)


# ADR-020 §8 — 변경 시 Ledger publish_config_change_breaking 호출 키.
# restricted_to 키와 겹치지만 (restricted_to ⊂ breaking), breaking 키는 운영 영향만 큰 경우도
# 포함 (예: citation.gates.generation.min_confidence — 일반 admin 가능하지만 audit chain 필요).
BREAKING_KEYS: frozenset[str] = frozenset(
    PLATFORM_ADMIN_RESTRICTED_KEYS
    | {
        "citation.gates.generation.min_confidence",
        "citation.verification.tier2.thresholds.strong",
        "citation.verification.tier2.thresholds.medium",
        "pii.input.on_pii_found.high_severity",     # 정책 변경
        "pii.storage.pii_storage_policy",           # plain 전환은 ADR-020 §4 별도 승인
        "compliance_mode",                          # standard|gdpr_strict|hipaa_strict 전이
    }
)


@dataclass(frozen=True)
class ConfigChangeRecord:
    tenant_id: str
    category: str
    key: str
    old_value: Any
    new_value: Any
    changed_by: str
    changed_at: datetime
    reason: str | None


class ConfigKeyRestrictedError(Exception):
    """restricted_to platform_admin 키를 일반 admin이 변경 시도."""

    def __init__(self, full_key: str):
        super().__init__(f"key requires platform_admin: {full_key}")
        self.full_key = full_key


class TenantConfigOverrideService:
    """ADR-009 §3·§8 tenant_config_overrides CRUD facade."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        ledger_audit=None,
    ) -> None:
        self._sf = session_factory
        self._ledger = ledger_audit

    @staticmethod
    def _full_key(category: str, key: str) -> str:
        return f"{category}.{key}"

    @staticmethod
    def is_platform_admin_restricted(category: str, key: str) -> bool:
        return f"{category}.{key}" in PLATFORM_ADMIN_RESTRICTED_KEYS

    @staticmethod
    def is_breaking(category: str, key: str) -> bool:
        return f"{category}.{key}" in BREAKING_KEYS

    async def patch(
        self,
        *,
        tenant_id: str,
        category: str,
        key: str,
        value: Any,
        actor: str,
        is_platform_admin: bool,
        reason: str | None = None,
    ) -> ConfigChangeRecord:
        full = self._full_key(category, key)

        if (
            self.is_platform_admin_restricted(category, key)
            and not is_platform_admin
        ):
            raise ConfigKeyRestrictedError(full)

        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)

            # 1) old_value (없으면 None)
            existing = (
                await session.execute(
                    text(
                        """
                        SELECT value FROM tenant_config_overrides
                         WHERE tenant_id = :tenant_id
                           AND category = :category
                           AND path = :path
                        """
                    ),
                    {"tenant_id": tenant_id, "category": category, "path": key},
                )
            ).first()
            old_value = existing[0] if existing else None

            # 2) UPSERT
            new_json = json.dumps(value, ensure_ascii=False, default=str)
            await session.execute(
                text(
                    """
                    INSERT INTO tenant_config_overrides (
                        tenant_id, category, path, value, reason, author
                    )
                    VALUES (
                        :tenant_id, :category, :path,
                        CAST(:value AS JSONB), :reason, :author
                    )
                    ON CONFLICT (tenant_id, category, path) DO UPDATE SET
                        value = EXCLUDED.value,
                        reason = EXCLUDED.reason,
                        author = EXCLUDED.author
                    """
                ),
                {
                    "tenant_id": tenant_id, "category": category, "path": key,
                    "value": new_json,
                    "reason": reason,
                    "author": actor,
                },
            )

            # 3) audit log row
            now_row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO tenant_config_change_logs (
                            tenant_id, category, path, old_value, new_value,
                            author, reason
                        )
                        VALUES (
                            :tenant_id, :category, :path,
                            CAST(:old_value AS JSONB), CAST(:new_value AS JSONB),
                            :author, :reason
                        )
                        RETURNING created_at
                        """
                    ),
                    {
                        "tenant_id": tenant_id, "category": category, "path": key,
                        "old_value": json.dumps(old_value, ensure_ascii=False, default=str)
                                     if old_value is not None else None,
                        "new_value": new_json,
                        "author": actor,
                        "reason": reason,
                    },
                )
            ).first()
            changed_at = now_row[0] if now_row else datetime.utcnow()
            await session.commit()

        # 4) Ledger publish on breaking key
        if self.is_breaking(category, key) and self._ledger is not None:
            try:
                await self._ledger.publish_config_change_breaking(
                    tenant_id=tenant_id, actor=actor,
                    category=category, path=key,
                    details={
                        "old_value": old_value,
                        "new_value": value,
                        "reason": reason,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ledger publish_config_change_breaking failed: %s", exc)

        # 5) cache invalidate
        TenantConfigService.invalidate(tenant_id)

        return ConfigChangeRecord(
            tenant_id=tenant_id, category=category, key=key,
            old_value=old_value, new_value=value,
            changed_by=actor, changed_at=changed_at, reason=reason,
        )

    async def reload(self, *, tenant_id: str) -> None:
        """수동 cache invalidate (POST /configs/reload)."""
        TenantConfigService.invalidate(tenant_id)

    async def list_history(
        self,
        *,
        tenant_id: str,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT category, path, old_value, new_value, author, reason, created_at
              FROM tenant_config_change_logs
             WHERE tenant_id = :tenant_id
        """
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit, "offset": offset}
        if category:
            sql += " AND category = :category"
            params["category"] = category
        sql += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

        async with self._sf() as session:
            await set_tenant_context(session, tenant_id)
            rows = (await session.execute(text(sql), params)).all()
            return [
                {
                    "category": r[0],
                    "path": r[1],
                    "old_value": dict(r[2]) if isinstance(r[2], dict) else r[2],
                    "new_value": dict(r[3]) if isinstance(r[3], dict) else r[3],
                    "author": r[4],
                    "reason": r[5],
                    "changed_at": r[6].isoformat() if r[6] else None,
                }
                for r in rows
            ]


# --------------------------------------------------------------------------- #
# InMemory variant — dev / tests
# --------------------------------------------------------------------------- #


class InMemoryTenantConfigOverrideService:
    """DB 없이 동작 — overrides는 dict, history는 list. ledger publish는 동일."""

    def __init__(self, *, ledger_audit=None) -> None:
        # (tenant_id, category, path) → value
        self._overrides: dict[tuple[str, str, str], Any] = {}
        self._history: list[dict[str, Any]] = []
        self._ledger = ledger_audit

    @staticmethod
    def is_platform_admin_restricted(category: str, key: str) -> bool:
        return TenantConfigOverrideService.is_platform_admin_restricted(category, key)

    @staticmethod
    def is_breaking(category: str, key: str) -> bool:
        return TenantConfigOverrideService.is_breaking(category, key)

    async def patch(
        self,
        *,
        tenant_id: str,
        category: str,
        key: str,
        value: Any,
        actor: str,
        is_platform_admin: bool,
        reason: str | None = None,
    ) -> ConfigChangeRecord:
        if (
            self.is_platform_admin_restricted(category, key)
            and not is_platform_admin
        ):
            raise ConfigKeyRestrictedError(f"{category}.{key}")
        old_value = self._overrides.get((tenant_id, category, key))
        self._overrides[(tenant_id, category, key)] = value
        record = ConfigChangeRecord(
            tenant_id=tenant_id, category=category, key=key,
            old_value=old_value, new_value=value,
            changed_by=actor, changed_at=datetime.utcnow(), reason=reason,
        )
        self._history.append(
            {
                "tenant_id": tenant_id, "category": category, "path": key,
                "old_value": old_value, "new_value": value,
                "author": actor, "reason": reason,
                "changed_at": record.changed_at.isoformat(),
            }
        )
        if self.is_breaking(category, key) and self._ledger is not None:
            try:
                await self._ledger.publish_config_change_breaking(
                    tenant_id=tenant_id, actor=actor,
                    category=category, path=key,
                    details={"old_value": old_value, "new_value": value, "reason": reason},
                )
            except Exception:  # noqa: BLE001
                pass
        TenantConfigService.invalidate(tenant_id)
        return record

    async def reload(self, *, tenant_id: str) -> None:
        TenantConfigService.invalidate(tenant_id)

    async def list_history(
        self,
        *,
        tenant_id: str,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        out = [h for h in self._history if h["tenant_id"] == tenant_id]
        if category:
            out = [h for h in out if h["category"] == category]
        out.reverse()
        return out[offset : offset + limit]
