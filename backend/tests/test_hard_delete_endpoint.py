"""DELETE /api/{domain_id}/admin/documents/{doc_id}/hard — cross-system 일관성 검증.

ADR-007/012:
  - Postgres chunks/documents 삭제
  - Qdrant points 삭제 (delete_points)
  - Storage 원본 파일 삭제 (LocalFilesystemStorage)
  - chat_logs_action 3종: keep_excerpts(default) / mask_excerpts / delete_logs
  - audit는 admin engine None(inmemory)이라 skip — 운영 환경에서만 검증
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.deps import (
    get_indexing_orchestrator,
    get_rag_service,
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_document_approval_service,
    reset_evaluation_orchestrator,
    reset_hard_delete_service,
    reset_indexing_orchestrator,
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


def _upload(client: TestClient, *, doc_id: str, content: bytes = b"hard delete target content"):
    return client.post(
        "/api/security/admin/documents/upload",
        files={"file": (f"{doc_id}.txt", io.BytesIO(content), "text/plain")},
        data={
            "doc_id": doc_id,
            "title": doc_id,
            "version": "v1",
            "metadata": '{"acl":["group:security"],"approval_status":"approved"}',
        },
        headers={"Authorization": "Bearer mock-token"},
    )


def test_hard_delete_removes_chunks_qdrant_storage_documents():
    from app.core.config import get_settings

    settings = get_settings()
    with TestClient(app) as client:
        up = _upload(client, doc_id="DOC-HD-1")
        assert up.status_code == 202, up.text

        # 사전 상태 확인 — orchestrator 인스턴스를 통해 검사
        orch = get_indexing_orchestrator(settings)
        vs = orch.service.vector_store
        coll_points = vs._collections["security"].points  # type: ignore[attr-defined]
        chunks_before = sum(
            1 for p in coll_points.values() if p.payload.get("doc_id") == "DOC-HD-1"
        )
        assert chunks_before >= 1

        resp = client.request(
            "DELETE",
            "/api/security/admin/documents/DOC-HD-1/hard",
            json={"reason": "compliance"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["removed_chunks"] == chunks_before
    assert body["removed_storage_files"] >= 1
    assert body["removed_documents"] == 1
    assert body["chat_logs_action_applied"] == "keep_excerpts"
    assert body["affected_chat_logs"] == 0
    assert body["dead_letters"] == []

    # 사후 — Qdrant points / storage 디렉토리 / documents 모두 비어있음
    coll_points = vs._collections["security"].points  # type: ignore[attr-defined]
    assert not any(p.payload.get("doc_id") == "DOC-HD-1" for p in coll_points.values())
    # documents repo
    doc = None
    import asyncio

    doc = asyncio.new_event_loop().run_until_complete(
        orch.service.document_repo.get(
            domain_id="security", doc_id="DOC-HD-1", version="v1"
        )
    )
    assert doc is None


def test_hard_delete_idempotent_when_doc_missing():
    """이미 존재하지 않는 doc_id 삭제 — 200 + 0건."""
    with TestClient(app) as client:
        resp = client.request(
            "DELETE",
            "/api/security/admin/documents/NOPE/hard",
            json={"reason": "test"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed_chunks"] == 0
    assert body["removed_documents"] == 0


def test_hard_delete_keep_excerpts_does_not_touch_chat_logs():
    """default 옵션 keep_excerpts — chat_logs 변동 없음."""
    from app.core.config import get_settings
    from rag_core.services.chat_log_writer import ChatLogPayload

    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]

    # chat_log 사전 삽입 (citations에 doc_id=DOC-HD-2 참조)
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        writer.write(
            ChatLogPayload(
                domain_id="security",
                request_id="r-prep",
                user_id="dev-user-001",
                conversation_id=None,
                question="질문",
                answer="답",
                citations=[
                    {"chunk_id": "c-x", "doc_id": "DOC-HD-2", "excerpt": "원본 발췌"}
                ],
            )
        )
    )

    with TestClient(app) as client:
        _upload(client, doc_id="DOC-HD-2")
        resp = client.request(
            "DELETE",
            "/api/security/admin/documents/DOC-HD-2/hard",
            json={"reason": "compliance"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["affected_chat_logs"] == 0

    # chat_logs.citations 원본 그대로
    log = next(r for r in writer.records if r.request_id == "r-prep")
    assert log.citations[0]["excerpt"] == "원본 발췌"


def test_hard_delete_mask_excerpts_masks_only_target_doc():
    """mask_excerpts — DOC-HD-3 citation만 마스킹, 다른 doc citation은 그대로."""
    from app.core.config import get_settings
    from rag_core.services.chat_log_writer import ChatLogPayload

    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        writer.write(
            ChatLogPayload(
                domain_id="security",
                request_id="r-mix",
                user_id="dev-user-001",
                conversation_id=None,
                question="q", answer="a",
                citations=[
                    {"chunk_id": "c1", "doc_id": "DOC-HD-3", "excerpt": "민감 발췌"},
                    {"chunk_id": "c2", "doc_id": "DOC-OTHER", "excerpt": "보존 발췌"},
                ],
            )
        )
    )

    with TestClient(app) as client:
        _upload(client, doc_id="DOC-HD-3")
        resp = client.request(
            "DELETE",
            "/api/security/admin/documents/DOC-HD-3/hard",
            json={"reason": "compliance", "chat_logs_action": "mask_excerpts"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["chat_logs_action_applied"] == "mask_excerpts"
    assert resp.json()["affected_chat_logs"] == 1

    log = next(r for r in writer.records if r.request_id == "r-mix")
    citations = {c["doc_id"]: c for c in log.citations}
    assert citations["DOC-HD-3"]["excerpt"] == "[masked: hard_deleted]"
    assert citations["DOC-OTHER"]["excerpt"] == "보존 발췌"


def test_hard_delete_delete_logs_removes_referencing_rows():
    """delete_logs — 해당 doc_id 참조 chat_logs row 자체 삭제."""
    from app.core.config import get_settings
    from rag_core.services.chat_log_writer import ChatLogPayload

    rag = get_rag_service(get_settings())
    writer = rag._deps.chat_log_writer  # type: ignore[attr-defined]
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        writer.write(
            ChatLogPayload(
                domain_id="security", request_id="r-del", user_id="u1",
                conversation_id=None, question="q", answer="a",
                citations=[{"chunk_id": "c", "doc_id": "DOC-HD-4"}],
            )
        )
    )
    asyncio.new_event_loop().run_until_complete(
        writer.write(
            ChatLogPayload(
                domain_id="security", request_id="r-keep", user_id="u1",
                conversation_id=None, question="q2", answer="a2",
                citations=[{"chunk_id": "c", "doc_id": "DOC-OTHER"}],
            )
        )
    )

    with TestClient(app) as client:
        _upload(client, doc_id="DOC-HD-4")
        resp = client.request(
            "DELETE",
            "/api/security/admin/documents/DOC-HD-4/hard",
            json={"reason": "compliance", "chat_logs_action": "delete_logs"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["affected_chat_logs"] == 1

    request_ids = {r.request_id for r in writer.records}
    assert "r-del" not in request_ids
    assert "r-keep" in request_ids
