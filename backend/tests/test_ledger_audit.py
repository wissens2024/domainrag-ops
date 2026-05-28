"""LedgerAuditService + hard_delete hookup 검증 (ADR-020 §8).

httpx 호출은 mock 처리. NoopLedgerClient.published 리스트로 publish 형식 확인.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app.deps import (
    get_ledger_audit_service,
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_document_approval_service,
    reset_document_metadata_service,
    reset_evaluation_orchestrator,
    reset_hard_delete_service,
    reset_indexing_orchestrator,
    reset_input_schema_service,
    reset_ledger_audit_service,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app
from app.services.ledger_audit_service import LedgerAuditService
from app.services.ledger_client import (
    HttpxLedgerClient,
    LedgerEvent,
    NoopLedgerClient,
)


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
    reset_document_metadata_service()
    reset_ledger_audit_service()
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
    reset_document_metadata_service()
    reset_ledger_audit_service()


# --------------------------------------------------------------------------- #
# Unit — LedgerAuditService
# --------------------------------------------------------------------------- #


async def test_publish_skipped_when_disabled():
    """enable=false면 client.publish 호출 안 함."""
    client = NoopLedgerClient()
    svc = LedgerAuditService(client=client, enable=False)
    ok = await svc.publish_hard_delete(
        domain_id="t1", actor="alice", reason="x",
        doc_id="d", version="v1",
        removed_chunks=1, affected_chat_logs=0,
        chat_logs_action="keep_excerpts",
    )
    assert ok is False
    assert client.published == []


async def test_publish_skipped_when_event_not_in_enabled_list():
    client = NoopLedgerClient()
    svc = LedgerAuditService(
        client=client, enable=True,
        enabled_events={"hard_delete"},  # auth_failure 제외
    )
    ok = await svc.publish_auth_failure(
        domain_id="t1", actor="bob", reason="invalid token"
    )
    assert ok is False
    assert client.published == []


async def test_publish_hard_delete_event_payload():
    client = NoopLedgerClient()
    svc = LedgerAuditService(client=client, enable=True)
    ok = await svc.publish_hard_delete(
        domain_id="security", actor="platform-admin-1",
        reason="compliance request", doc_id="DOC-1", version="v1",
        removed_chunks=5, affected_chat_logs=2,
        chat_logs_action="mask_excerpts",
    )
    assert ok is True
    assert len(client.published) == 1
    event = client.published[0]
    assert event.event_type == "hard_delete"
    assert event.domain_id == "security"
    assert event.actor == "platform-admin-1"
    assert event.reason == "compliance request"
    assert event.details == {
        "doc_id": "DOC-1", "version": "v1",
        "removed_chunks": 5, "affected_chat_logs": 2,
        "chat_logs_action": "mask_excerpts",
    }
    # to_payload — source_system 명시
    payload = event.to_payload()
    assert payload["source_system"] == "domainrag"
    assert "timestamp" in payload


async def test_publish_platform_admin_action_event():
    client = NoopLedgerClient()
    svc = LedgerAuditService(client=client, enable=True)
    await svc.publish_platform_admin_action(
        domain_id="security", actor="root", action="pii_storage_plain_approved",
        details={"approval_id": "X"},
    )
    assert client.published[0].event_type == "platform_admin_action"
    assert client.published[0].details["action"] == "pii_storage_plain_approved"
    assert client.published[0].details["approval_id"] == "X"


async def test_publish_tenant_mismatch_merges_expected_token_fields():
    client = NoopLedgerClient()
    svc = LedgerAuditService(client=client, enable=True)
    await svc.publish_tenant_mismatch(
        domain_id="security", actor="user-9",
        expected_tenant="security", token_tenant="legal",
    )
    e = client.published[0]
    assert e.event_type == "tenant_mismatch"
    assert e.details["expected_tenant"] == "security"
    assert e.details["token_tenant"] == "legal"


# --------------------------------------------------------------------------- #
# HttpxLedgerClient — 실패 swallow
# --------------------------------------------------------------------------- #


async def test_httpx_ledger_client_swallows_connection_error():
    """포트 미응답 시 raise하지 않고 False 반환."""
    client = HttpxLedgerClient(
        endpoint="http://127.0.0.1:1",  # 거의 확실히 닫힌 포트
        api_key="x",
        timeout_seconds=0.5,
        max_retries=0,
    )
    ok = await client.publish(
        LedgerEvent(
            event_type="hard_delete", domain_id="t1",
            actor="a", reason="r", details={}, timestamp="2026-05-12T00:00:00+00:00",
        )
    )
    assert ok is False


# --------------------------------------------------------------------------- #
# Endpoint hookup — hard_delete 호출 시 ledger publish
# --------------------------------------------------------------------------- #


def test_hard_delete_endpoint_publishes_to_ledger():
    """hard_delete endpoint가 LedgerAuditService.publish_hard_delete를 호출하는지 검증.

    구현 의존성을 우회해 LedgerAuditService 싱글톤을 미리 enable=True + NoopClient로
    셋업한다. hard_delete singleton도 같은 ledger_audit을 받도록 함께 reset.
    """
    import app.deps as deps
    from app.services.ledger_audit_service import LedgerAuditService

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)

    with TestClient(app) as client:
        up = client.post(
            "/api/security/admin/documents/upload",
            files={"file": ("p.txt", io.BytesIO(b"text"), "text/plain")},
            data={"doc_id": "DOC-L1", "title": "ledger-target", "version": "v1",
                  "metadata": '{"acl":["group:security"],"approval_status":"approved"}'},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert up.status_code == 202

        resp = client.request(
            "DELETE",
            "/api/security/admin/documents/DOC-L1/hard",
            json={"reason": "ledger test", "chat_logs_action": "keep_excerpts"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text

    hard_delete_events = [
        e for e in noop.published if e.event_type == "hard_delete"
    ]
    assert len(hard_delete_events) == 1
    e = hard_delete_events[0]
    assert e.domain_id == "security"
    assert e.details["doc_id"] == "DOC-L1"
    assert e.details["chat_logs_action"] == "keep_excerpts"
    assert e.details["removed_chunks"] >= 1


def test_pii_storage_approval_publishes_platform_admin_action():
    """PII plain 승인 + 회수 → Ledger에 platform_admin_action 2건."""
    import app.deps as deps
    from app.core.auth_adapter import UserContext, get_user_context
    from app.services.ledger_audit_service import LedgerAuditService

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    # pii_approval_service 빌드 시 ledger를 받도록 reset → 다시 빌드
    deps._pii_approval_service = None

    def _platform_admin():
        return UserContext(
            user_id="platform-admin-001",
            domain_id="platform",
            roles=["PLATFORM_ADMIN"],
            clearance="secret",
        )

    app.dependency_overrides[get_user_context] = _platform_admin

    with TestClient(app) as client:
        client.post(
            "/api/platform/admin/tenants/security/pii-storage-approvals",
            json={"reason": "compliance"},
        )
        client.request(
            "DELETE",
            "/api/platform/admin/tenants/security/pii-storage-approvals/active",
            json={"reason": "revoke"},
        )

    actions = [
        e for e in noop.published if e.event_type == "platform_admin_action"
    ]
    assert len(actions) == 2
    types = sorted(e.details["action"] for e in actions)
    assert types == ["pii_storage_plain_approved", "pii_storage_plain_revoked"]
    for e in actions:
        assert e.domain_id == "security"
        assert e.actor == "platform-admin-001"


def test_tenant_mismatch_publishes_to_ledger():
    """non-platform-admin user가 다른 tenant 호출 시 publish_tenant_mismatch."""
    import app.deps as deps
    from app.core.auth_adapter import UserContext, get_user_context
    from app.services.ledger_audit_service import LedgerAuditService

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)

    def _user_in_other_tenant():
        return UserContext(
            user_id="user-99",
            domain_id="legal",  # 토큰은 legal
            roles=["USER", "ADMIN"],
            clearance="confidential",
        )

    app.dependency_overrides[get_user_context] = _user_in_other_tenant

    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/documents",  # path는 security
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "tenant_mismatch"

    events = [e for e in noop.published if e.event_type == "tenant_mismatch"]
    assert len(events) == 1
    e = events[0]
    assert e.domain_id == "security"
    assert e.actor == "user-99"
    assert e.details["expected_tenant"] == "security"
    assert e.details["token_tenant"] == "legal"


def test_platform_admin_does_not_trigger_tenant_mismatch_publish():
    """platform_admin은 path와 token tenant 달라도 mismatch 미발생."""
    import app.deps as deps
    from app.core.auth_adapter import UserContext, get_user_context
    from app.services.ledger_audit_service import LedgerAuditService

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)

    def _platform_admin():
        return UserContext(
            user_id="platform-admin-001",
            domain_id="platform",
            roles=["PLATFORM_ADMIN"],
            clearance="secret",
        )

    app.dependency_overrides[get_user_context] = _platform_admin
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/documents",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    assert all(e.event_type != "tenant_mismatch" for e in noop.published)


def test_chat_streaming_pii_block_publishes_to_ledger():
    """chat_streaming도 input_pii_blocked 시 publish_pii_high_severity_block(ui_mode=chat_streaming)."""
    import app.deps as deps
    from app.services.ledger_audit_service import LedgerAuditService

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    deps._rag_service = None  # 재빌드해 streaming_service에 ledger 주입

    with TestClient(app) as client:
        resp = client.post(
            "/api/security/chat/stream",
            json={
                "conversation_id": None,
                "question": "내 주민번호는 901231-1234567 입니다",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    assert "input_pii_blocked" in resp.text

    events = [
        e for e in noop.published
        if e.event_type == "pii_high_severity_block"
        and e.details.get("ui_mode") == "chat_streaming"
    ]
    assert len(events) == 1
    assert "rrn" in events[0].details["blocked_categories"]


def test_pii_high_severity_block_publishes_to_ledger():
    """chat 응답이 input_pii_blocked면 publish_pii_high_severity_block."""
    import app.deps as deps
    from app.services.ledger_audit_service import LedgerAuditService

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    deps._rag_service = None  # RAGService 재빌드해 ledger 주입

    with TestClient(app) as client:
        resp = client.post(
            "/api/security/chat",
            json={
                "conversation_id": None,
                # 주민번호 패턴 — high severity → block
                "question": "내 주민번호는 901231-1234567 입니다",
            },
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "fallback"
    assert body["fallback"]["reason"] == "input_pii_blocked"

    events = [
        e for e in noop.published if e.event_type == "pii_high_severity_block"
    ]
    assert len(events) == 1
    e = events[0]
    assert e.domain_id == "security"
    assert e.actor == "dev-user-001"  # mock default user
    assert "rrn" in e.details["blocked_categories"]


def test_evaluation_promote_publishes_platform_admin_action():
    """evaluation promote → Ledger publish_platform_admin_action(action=evaluation_promoted)."""
    import asyncio

    import app.deps as deps
    from app.core.config import get_settings
    from app.deps import get_evaluation_orchestrator
    from app.services.ledger_audit_service import LedgerAuditService
    from rag_core.interfaces.evaluation_job_repository import EvaluationJobRecord

    noop = NoopLedgerClient()
    deps._ledger_audit_service = LedgerAuditService(client=noop, enable=True)
    deps._evaluation_orchestrator = None  # 재빌드 강제

    orch = get_evaluation_orchestrator(get_settings())
    # gate 통과한 job을 직접 주입 — promote 자격
    asyncio.new_event_loop().run_until_complete(
        orch.repo.create(
            EvaluationJobRecord(
                job_id="EVAL-PROMOTE-001",
                domain_id="security",
                dataset_name="tenant_security",
                status="completed",
                actor="seed",
                summary={"total_cases": 5},
                gate_result={"passed": True, "metrics": []},
            )
        )
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/evaluation/jobs/EVAL-PROMOTE-001/promote",
            json={"target": "model", "version": "qwen3-7b-v2"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text

    events = [
        e for e in noop.published
        if e.event_type == "platform_admin_action"
        and e.details.get("action") == "evaluation_promoted"
    ]
    assert len(events) == 1
    e = events[0]
    assert e.domain_id == "security"
    assert e.details["job_id"] == "EVAL-PROMOTE-001"
    assert e.details["target"] == "model"
    assert e.details["version"] == "qwen3-7b-v2"
