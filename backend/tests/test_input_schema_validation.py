"""ADR-015 input_schema 검증 — validator unit + upload/list endpoint e2e.

seed: configs/platform/common_fields.yaml + configs/tenants/security/input_schema.yaml.
보안 tenant의 `policy_document`는 policy_id(pattern SEC-POL-NNN), policy_type(enum),
department(enum), authority_rank(int 0~100), effective_date(date) 필수.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.input_schema import (
    InputSchemaLoader,
    InputSchemaService,
    InputSchemaValidationError,
)
from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_document_approval_service,
    reset_evaluation_orchestrator,
    reset_hard_delete_service,
    reset_indexing_orchestrator,
    reset_input_schema_service,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


@pytest.fixture(autouse=True)
def _reset():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()
    reset_document_approval_service()
    reset_hard_delete_service()
    reset_input_schema_service()
    yield
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()
    reset_document_approval_service()
    reset_hard_delete_service()
    reset_input_schema_service()


def _service() -> InputSchemaService:
    settings = get_settings()
    return InputSchemaService(config_dir=settings.config_dir.resolve())


VALID_POLICY_META = {
    "title": "패스워드 정책 v1",
    "security_level": "internal",
    "acl": ["group:security"],
    "owner": "alice",
    "policy_id": "SEC-POL-100",
    "policy_type": "access_control",
    "department": "security",
    "authority_rank": 80,
    "effective_date": "2026-05-01",
}


# --------------------------------------------------------------------------- #
# Unit — validator
# --------------------------------------------------------------------------- #


def test_validate_passes_for_complete_metadata():
    """필수 + domain 필드 모두 충족 → 예외 없음."""
    _service().validate(
        tenant_id="security",
        input_type="policy_document",
        metadata=VALID_POLICY_META,
    )


def test_validate_returns_missing_for_required_field():
    bad = dict(VALID_POLICY_META)
    del bad["policy_id"]
    with pytest.raises(InputSchemaValidationError) as exc:
        _service().validate(
            tenant_id="security", input_type="policy_document", metadata=bad
        )
    paths = [e.path for e in exc.value.errors]
    codes = [e.code for e in exc.value.errors]
    assert "metadata.policy_id" in paths
    assert "missing" in codes


def test_validate_enforces_pattern_on_policy_id():
    bad = {**VALID_POLICY_META, "policy_id": "WRONG-ID"}
    with pytest.raises(InputSchemaValidationError) as exc:
        _service().validate(
            tenant_id="security", input_type="policy_document", metadata=bad
        )
    assert any(
        e.code == "pattern" and e.path == "metadata.policy_id"
        for e in exc.value.errors
    )


def test_validate_enforces_enum_on_policy_type():
    bad = {**VALID_POLICY_META, "policy_type": "not_a_valid_policy_type"}
    with pytest.raises(InputSchemaValidationError) as exc:
        _service().validate(
            tenant_id="security", input_type="policy_document", metadata=bad
        )
    assert any(
        e.code == "enum" and e.path == "metadata.policy_type"
        for e in exc.value.errors
    )


def test_validate_enforces_integer_range_on_authority_rank():
    bad = {**VALID_POLICY_META, "authority_rank": 150}
    with pytest.raises(InputSchemaValidationError) as exc:
        _service().validate(
            tenant_id="security", input_type="policy_document", metadata=bad
        )
    assert any(e.code == "range" for e in exc.value.errors)


def test_validate_enforces_date_format_on_effective_date():
    bad = {**VALID_POLICY_META, "effective_date": "not-a-date"}
    with pytest.raises(InputSchemaValidationError) as exc:
        _service().validate(
            tenant_id="security", input_type="policy_document", metadata=bad
        )
    assert any(e.code == "format" for e in exc.value.errors)


def test_validate_unknown_input_type_raises():
    with pytest.raises(InputSchemaValidationError) as exc:
        _service().validate(
            tenant_id="security",
            input_type="ghost_type",
            metadata=VALID_POLICY_META,
        )
    assert exc.value.errors[0].code == "unknown_input_type"


def test_validate_skips_when_input_type_none():
    """input_type=None이면 skip — 기존 upload 흐름 회귀 방지."""
    _service().validate(
        tenant_id="security", input_type=None, metadata={}
    )


# --------------------------------------------------------------------------- #
# Endpoint — /input_schemas + upload 422
# --------------------------------------------------------------------------- #


def test_list_input_schemas_returns_known_types():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/input_schemas",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {t["name"] for t in body["input_types"]}
    assert "policy_document" in names
    # 각 schema는 JSON Schema 형식 + required 포함
    policy = next(t for t in body["input_types"] if t["name"] == "policy_document")
    assert "required" in policy["schema"]
    assert "policy_id" in policy["schema"]["required"]


def test_upload_with_valid_input_type_passes_validation():
    """metadata에 모든 필수 필드 충족 시 upload 202."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("p.txt", io.BytesIO(b"text"), "text/plain")},
            data={
                "doc_id": "DOC-V1",
                "title": "패스워드 정책 v1",
                "version": "v1",
                "input_type": "policy_document",
                "metadata": json.dumps(
                    {
                        "security_level": "internal",
                        "acl": ["group:security"],
                        "owner": "alice",
                        "policy_id": "SEC-POL-100",
                        "policy_type": "access_control",
                        "department": "security",
                        "authority_rank": 80,
                        "effective_date": "2026-05-01",
                    }
                ),
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 202, resp.text


def test_upload_with_missing_required_field_returns_422():
    """필수 policy_id 누락 → 422 + fields[].path 정보."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("p.txt", io.BytesIO(b"text"), "text/plain")},
            data={
                "doc_id": "DOC-V2",
                "title": "정책",
                "version": "v1",
                "input_type": "policy_document",
                "metadata": json.dumps(
                    {
                        "security_level": "internal",
                        "acl": ["group:security"],
                        "owner": "alice",
                        # policy_id 누락
                        "policy_type": "access_control",
                        "department": "security",
                        "authority_rank": 50,
                        "effective_date": "2026-05-01",
                    }
                ),
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "input_schema_validation_failed"
    assert detail["input_type"] == "policy_document"
    paths = [f["path"] for f in detail["fields"]]
    assert "metadata.policy_id" in paths


def test_upload_with_unknown_input_type_returns_422():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("p.txt", io.BytesIO(b"text"), "text/plain")},
            data={
                "doc_id": "DOC-V3",
                "title": "정책",
                "version": "v1",
                "input_type": "no_such_type",
                "metadata": "{}",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["fields"][0]["code"] == "unknown_input_type"
