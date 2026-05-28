"""InputSchemaService — ADR-015 input_schema 로드 + light validator.

운영 원칙:
  - source: configs/tenants/<domain_id>/input_schema.yaml (도메인 필드)
  - inherits: configs/platform/common_fields.yaml (공통 필드)
  - 본 service가 두 yaml을 합성해 input_type별 JSON Schema 형태 dict를 반환

지원하는 JSON Schema keyword (운영 요구 충분):
  - type: string / integer / number / boolean / array / object
    + ['string','null'] 같은 union type
  - required: list[str]  (필드 단위 required: true 또는 common_required)
  - enum: list
  - pattern: regex (string)
  - minimum / maximum (integer/number)
  - min_length / max_length (string)
  - format: date / date-time / email (간단)
  - items: child schema (array)
  - min_items / max_items (array)

복잡한 $ref·oneOf·allOf는 본 작업 범위 외 — common_fields 상속은 별도 inherits 키로 처리.

장기적으로 jsonschema 라이브러리로 교체 가능 (Protocol 동일 유지).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any

import yaml


@dataclass
class FieldError:
    """필드 단위 검증 실패 — 422 응답에 그대로 포함된다."""

    path: str  # 예: "metadata.policy_id" 또는 "metadata.tags[0]"
    code: str  # missing | type | enum | pattern | range | length | format | unknown_input_type
    message: str


class InputSchemaValidationError(Exception):
    """422 변환용. errors는 caller가 직렬화한다."""

    def __init__(self, errors: list[FieldError]):
        self.errors = errors
        if errors:
            summary = ", ".join(
                f"{e.path}[{e.code}]" for e in errors[:5]
            )
            super().__init__(f"{len(errors)} field error(s): {summary}")
        else:
            super().__init__("0 field error(s)")


@dataclass
class InputTypeSchema:
    """input_type별 합성된 schema (common + domain)."""

    name: str
    display_name: str | None = None
    required: list[str] = field(default_factory=list)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_json_schema(self) -> dict[str, Any]:
        """ADR-017 §6.4 endpoint에서 반환할 JSON Schema 형식."""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.display_name or self.name,
            "type": "object",
            "required": list(self.required),
            "properties": dict(self.fields),
        }


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #


class InputSchemaLoader:
    """tenant input_schema 로드 + 캐시.

    common_fields.yaml의 common_required + common_field_definitions를 tenant input_type
    domain_fields와 합성. tenant override(required, type 등)가 common 위에 적용된다.
    """

    _cache: dict[str, dict[str, InputTypeSchema]] = {}
    # ADR-017 §15 — SchemaEditorService PUT가 적재. 이 layer가 disk yaml보다 우선.
    _runtime_yaml: dict[str, dict] = {}
    _lock = Lock()

    @classmethod
    def apply_runtime_override(cls, domain_id: str, schema_yaml: dict) -> None:
        """ADR-017 §15 — Schema Editor PUT 후 즉시 validation에 반영.

        Schema Editor PUT은 ADR-015 §1 list 포맷을 검증하지만 disk yaml + InputSchemaLoader는
        dict 포맷을 사용한다. 두 포맷을 모두 받아 dict로 정규화해 저장한다.
        list 항목은 `name` 키를 dict key로 사용.
        """
        normalized = dict(schema_yaml or {})
        input_types = normalized.get("input_types")
        if isinstance(input_types, list):
            as_dict: dict[str, dict] = {}
            for it in input_types:
                if not isinstance(it, dict):
                    continue
                name = it.get("name")
                if not name:
                    continue
                # name 키 자체는 dict 키로 들어가므로 제외, 나머지 보존
                as_dict[str(name)] = {k: v for k, v in it.items() if k != "name"}
            normalized["input_types"] = as_dict
        with cls._lock:
            cls._runtime_yaml[domain_id] = normalized
            cls._cache.pop(domain_id, None)

    @classmethod
    def clear_runtime_override(cls, domain_id: str | None = None) -> None:
        """테스트용 — runtime layer 초기화."""
        with cls._lock:
            if domain_id is None:
                cls._runtime_yaml.clear()
            else:
                cls._runtime_yaml.pop(domain_id, None)
            cls._cache.pop(domain_id, None) if domain_id else cls._cache.clear()

    @classmethod
    def load(cls, *, config_dir: Path, domain_id: str) -> dict[str, InputTypeSchema]:
        with cls._lock:
            cached = cls._cache.get(domain_id)
            if cached is not None:
                return cached

        common_path = config_dir / "platform" / "common_fields.yaml"
        common = _read_yaml(common_path) if common_path.exists() else {}

        # 1) runtime override 우선 (Schema Editor PUT 결과)
        runtime = cls._runtime_yaml.get(domain_id)
        if runtime is not None:
            tenant = runtime
        else:
            tenant_path = config_dir / "tenants" / domain_id / "input_schema.yaml"
            if not tenant_path.exists():
                with cls._lock:
                    cls._cache[domain_id] = {}
                return {}
            tenant = _read_yaml(tenant_path)

        common_required = list(common.get("common_required") or [])
        common_fields = dict(common.get("common_field_definitions") or {})

        # domain_id는 자동 주입 — 사용자 입력 검증 대상 아님
        common_required = [r for r in common_required if r != "domain_id"]
        common_fields.pop("domain_id", None)
        # input_type도 endpoint Form 필드에서 별도 검증 — body의 metadata에서는 다루지 않음
        common_required = [r for r in common_required if r != "input_type"]
        common_fields.pop("input_type", None)

        result: dict[str, InputTypeSchema] = {}
        for type_name, type_def in (tenant.get("input_types") or {}).items():
            domain_fields = dict(type_def.get("domain_fields") or {})
            # domain_fields의 required: true 추출
            domain_required = [
                fname for fname, fdef in domain_fields.items()
                if isinstance(fdef, dict) and fdef.get("required") is True
            ]
            # 합성: common 먼저, domain이 override
            merged_fields = {**common_fields, **domain_fields}
            merged_required = list(dict.fromkeys(common_required + domain_required))
            result[type_name] = InputTypeSchema(
                name=type_name,
                display_name=type_def.get("display_name"),
                required=merged_required,
                fields=merged_fields,
            )

        with cls._lock:
            cls._cache[domain_id] = result
        return result

    @classmethod
    def reset(cls, domain_id: str | None = None) -> None:
        with cls._lock:
            if domain_id is None:
                cls._cache.clear()
            else:
                cls._cache.pop(domain_id, None)


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #


class InputSchemaService:
    """ADR-015 검증 진입점. caller는 validate(domain_id, input_type, metadata) 호출."""

    def __init__(self, *, config_dir: Path) -> None:
        self._config_dir = config_dir

    def list_input_types(self, domain_id: str) -> list[InputTypeSchema]:
        return list(InputSchemaLoader.load(
            config_dir=self._config_dir, domain_id=domain_id
        ).values())

    def get_schema(self, domain_id: str, input_type: str) -> InputTypeSchema | None:
        return InputSchemaLoader.load(
            config_dir=self._config_dir, domain_id=domain_id
        ).get(input_type)

    def validate(
        self,
        *,
        domain_id: str,
        input_type: str | None,
        metadata: dict[str, Any],
    ) -> None:
        """metadata가 input_type schema를 충족하지 못하면 InputSchemaValidationError raise.

        input_type이 None이면 검증 skip(운영자가 명시적으로 input_type 미선택한 경우 —
        endpoint 측에서 None 허용 여부를 결정).
        """
        if input_type is None:
            return
        schemas = InputSchemaLoader.load(
            config_dir=self._config_dir, domain_id=domain_id
        )
        schema = schemas.get(input_type)
        if schema is None:
            raise InputSchemaValidationError([
                FieldError(
                    path="input_type",
                    code="unknown_input_type",
                    message=f"input_type '{input_type}'은 tenant '{domain_id}'에 정의되지 않음",
                )
            ])

        errors: list[FieldError] = []
        # required check
        for r in schema.required:
            if metadata.get(r) in (None, "", []):
                errors.append(
                    FieldError(
                        path=f"metadata.{r}",
                        code="missing",
                        message=f"필수 필드 누락: {r}",
                    )
                )
        # 각 필드 type / enum / pattern / range
        for fname, fdef in schema.fields.items():
            if not isinstance(fdef, dict):
                continue
            if fname not in metadata or metadata[fname] in (None, ""):
                continue
            _validate_field(fname, metadata[fname], fdef, errors)

        if errors:
            raise InputSchemaValidationError(errors)


def _validate_field(
    fname: str, value: Any, fdef: dict[str, Any], errors: list[FieldError]
) -> None:
    """단일 필드 검증 — 위반은 errors에 append."""
    path = f"metadata.{fname}"
    expected = fdef.get("type")
    if expected and not _type_match(value, expected):
        errors.append(
            FieldError(
                path=path, code="type",
                message=f"기대 type {expected!r}, 실제 {type(value).__name__}",
            )
        )
        return  # type 위반이면 후속 검증 의미 없음

    if "enum" in fdef and value not in fdef["enum"]:
        errors.append(
            FieldError(
                path=path, code="enum",
                message=f"허용 값이 아님 (enum={fdef['enum']})",
            )
        )

    if "pattern" in fdef and isinstance(value, str):
        # JSON Schema 표준 — substring match (^,$ 없으면 partial). re.search 사용.
        if not re.search(fdef["pattern"], value):
            errors.append(
                FieldError(
                    path=path, code="pattern",
                    message=f"패턴 위반 (pattern={fdef['pattern']})",
                )
            )

    if isinstance(value, int) and not isinstance(value, bool):
        lo = fdef.get("minimum")
        hi = fdef.get("maximum")
        if lo is not None and value < lo:
            errors.append(
                FieldError(path=path, code="range", message=f"{value} < minimum {lo}")
            )
        if hi is not None and value > hi:
            errors.append(
                FieldError(path=path, code="range", message=f"{value} > maximum {hi}")
            )

    if isinstance(value, str):
        lo = fdef.get("min_length") or fdef.get("minLength")
        hi = fdef.get("max_length") or fdef.get("maxLength")
        if lo is not None and len(value) < lo:
            errors.append(
                FieldError(path=path, code="length", message=f"길이 {len(value)} < {lo}")
            )
        if hi is not None and len(value) > hi:
            errors.append(
                FieldError(path=path, code="length", message=f"길이 {len(value)} > {hi}")
            )
        fmt = fdef.get("format")
        if fmt and not _format_ok(value, fmt):
            errors.append(
                FieldError(path=path, code="format", message=f"format {fmt!r} 위반")
            )

    if isinstance(value, list):
        items_def = fdef.get("items")
        if isinstance(items_def, dict):
            for i, item in enumerate(value):
                _validate_field(f"{fname}[{i}]", item, items_def, errors)
        min_items = fdef.get("min_items") or fdef.get("minItems")
        if min_items is not None and len(value) < min_items:
            errors.append(
                FieldError(
                    path=path, code="length",
                    message=f"min_items {min_items}, got {len(value)}",
                )
            )


def _type_match(value: Any, expected: Any) -> bool:
    """expected는 단일 str 또는 list[str]. null 허용도 지원."""
    if isinstance(expected, list):
        # ['string', 'null'] 같은 union
        for e in expected:
            if e == "null" and value is None:
                return True
            if _type_match(value, e):
                return True
        return False
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        # bool은 python int 하위 — JSON 의미상 분리
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    # 알 수 없는 type — 통과 (forward compat)
    return True


def _format_ok(value: str, fmt: str) -> bool:
    """format keyword 간단 구현 — date / date-time / email."""
    try:
        if fmt == "date":
            date.fromisoformat(value)
            return True
        if fmt in ("date-time", "datetime"):
            # python fromisoformat는 'Z' 미지원 — 'Z'를 '+00:00'으로 변환
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        if fmt == "email":
            return "@" in value and "." in value.split("@", 1)[-1]
    except (ValueError, AttributeError):
        return False
    return True
