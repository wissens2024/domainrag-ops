"""SchemaEditorService — ADR-017 §15 + ADR-015 schema 편집 orchestrator.

흐름:
  - GET admin/schema: repository.get_active(tenant_id) → record + schema_version
  - PUT admin/schema: backward compat 검증 → repository.insert_new_active(base_version)
  - GET admin/schema/history: repository.list_history(tenant_id)

Backward compat 룰 (ADR-015 §2 보강):
  - input_types 배열이 존재해야 함 (기본 구조 검증)
  - 이전 active의 input_type name이 사라지면 차단 — 기존 documents의 input_type 참조 깨짐
  - 다른 변경(필드 추가/제거)은 경고 없이 허용 — frontend RJSF가 missing field를 null로 처리

향후 강화 가능: required 필드 추가 시 default 동반 강제, type 변경 차단 등.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rag_core.interfaces.tenant_input_schema_repository import (
    TenantInputSchemaRecord,
    TenantInputSchemaRepository,
)


class SchemaBackwardCompatError(Exception):
    """backward compat 위반 — 사용자에게 errors 목록과 함께 422 반환."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class SchemaPutResult:
    record: TenantInputSchemaRecord
    deprecated_version: int | None


class SchemaEditorService:
    def __init__(
        self,
        *,
        repo: TenantInputSchemaRepository,
        runtime_apply=None,
    ) -> None:
        """Args:
            repo: tenant_input_schemas CRUD.
            runtime_apply: 선택적 hook — put 성공 후 호출되어 InputSchemaService
                (또는 동등한 layer)에 새 schema를 즉시 반영. (ADR-017 §15 follow-up)
        """
        self._repo = repo
        self._runtime_apply = runtime_apply

    async def get_active(
        self, *, tenant_id: str
    ) -> TenantInputSchemaRecord | None:
        return await self._repo.get_active(tenant_id=tenant_id)

    async def list_history(
        self, *, tenant_id: str, limit: int = 50, offset: int = 0
    ) -> list[TenantInputSchemaRecord]:
        return await self._repo.list_history(
            tenant_id=tenant_id, limit=limit, offset=offset
        )

    async def put(
        self,
        *,
        tenant_id: str,
        base_version: int | None,
        schema_yaml: dict[str, Any],
        ui_schema_yaml: dict[str, Any] | None,
    ) -> SchemaPutResult:
        # 1. 기본 구조 검증
        errors = _validate_structure(schema_yaml)
        if errors:
            raise SchemaBackwardCompatError(errors)

        # 2. backward compat — 기존 input_type 제거 차단
        current = await self._repo.get_active(tenant_id=tenant_id)
        if current is not None:
            removed = _removed_input_types(current.schema_yaml, schema_yaml)
            if removed:
                raise SchemaBackwardCompatError(
                    [f"input_type_removed: {name}" for name in removed]
                )

        # 3. insert (optimistic lock은 repo가 검증)
        record = await self._repo.insert_new_active(
            tenant_id=tenant_id,
            base_version=base_version,
            schema_yaml=schema_yaml,
            ui_schema_yaml=ui_schema_yaml or {},
        )
        # 4. InputSchemaService 즉시 반영 (best-effort)
        if self._runtime_apply is not None:
            try:
                self._runtime_apply(tenant_id, schema_yaml)
            except Exception:  # noqa: BLE001
                pass  # validation에 반영 못해도 DB는 갱신됨 — 다음 reload 시 반영
        return SchemaPutResult(
            record=record,
            deprecated_version=current.schema_version if current else None,
        )


def _validate_structure(yaml_value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(yaml_value, dict):
        return ["schema_yaml_not_dict"]
    input_types = yaml_value.get("input_types")
    if input_types is None:
        errors.append("input_types_missing")
    elif not isinstance(input_types, list):
        errors.append("input_types_not_list")
    else:
        for i, it in enumerate(input_types):
            if not isinstance(it, dict):
                errors.append(f"input_types[{i}]_not_dict")
                continue
            if not it.get("name"):
                errors.append(f"input_types[{i}]_missing_name")
    return errors


def _removed_input_types(
    old_yaml: dict[str, Any], new_yaml: dict[str, Any]
) -> list[str]:
    old_names = _collect_names(old_yaml)
    new_names = _collect_names(new_yaml)
    return sorted(old_names - new_names)


def _collect_names(yaml_value: dict[str, Any]) -> set[str]:
    items = yaml_value.get("input_types") or []
    out: set[str] = set()
    for it in items:
        if isinstance(it, dict) and it.get("name"):
            out.add(str(it["name"]))
    return out
