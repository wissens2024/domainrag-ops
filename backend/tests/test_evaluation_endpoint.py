"""/api/{domain_id}/admin/evaluation/* e2e (ADR-017 §16).

env: AUTH_MODE=mock + RAG_BACKEND=inmemory. RAGService의 InMemory deps와 vector store/
embedder를 공유해 chat_structured_full 그래프로 평가를 실행한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.deps import (
    reset_authfusion_token_client,
    reset_chat_log_eraser,
    reset_evaluation_orchestrator,
    reset_indexing_orchestrator,
    reset_oauth_state_store,
    reset_pii_approval_service,
    reset_rag_service,
)
from app.main import app


@pytest.fixture(autouse=True)
def _reset_all():
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()
    yield
    app.dependency_overrides.clear()
    reset_rag_service()
    reset_indexing_orchestrator()
    reset_pii_approval_service()
    reset_chat_log_eraser()
    reset_oauth_state_store()
    reset_authfusion_token_client()
    reset_evaluation_orchestrator()


def test_list_datasets_returns_platform_and_tenant():
    """data/eval에 seed된 platform_smoke + tenant_security 모두 반환."""
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/evaluation/datasets",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    names = {d["name"] for d in resp.json()["datasets"]}
    assert "platform_smoke" in names
    assert "tenant_security" in names


def test_run_evaluation_starts_background_and_completes():
    """run 202 + background 종료 후 job status=completed/summary 채워짐."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/evaluation/run",
            json={"dataset_name": "tenant_security"},
            headers={"Authorization": "Bearer mock-token"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        job_id = body["job_id"]
        assert body["status"] == "pending"

        # background 종료 후 조회 (TestClient는 background 완료까지 대기)
        detail = client.get(
            f"/api/security/admin/evaluation/jobs/{job_id}",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert detail.status_code == 200, detail.text
    job = detail.json()
    assert job["status"] == "completed", job
    summary = job["summary"]
    assert summary["total_cases"] == 5
    assert summary["retrieval_recall_at_5"] == 1.0
    # security promotion_gate 평가도 함께 — gate_result 채워져 있어야 한다
    assert "passed" in job["gate_result"]
    assert isinstance(job["gate_result"]["metrics"], list)


def test_run_unknown_dataset_returns_404():
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/evaluation/run",
            json={"dataset_name": "nope"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "dataset_not_found"


def test_get_unknown_job_returns_404():
    with TestClient(app) as client:
        resp = client.get(
            "/api/security/admin/evaluation/jobs/EVAL-NOPE",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "evaluation_job_not_found"


def test_list_evaluation_jobs_returns_recent_first():
    with TestClient(app) as client:
        client.post(
            "/api/security/admin/evaluation/run",
            json={"dataset_name": "platform_smoke"},
            headers={"Authorization": "Bearer mock-token"},
        )
        client.post(
            "/api/security/admin/evaluation/run",
            json={"dataset_name": "tenant_security"},
            headers={"Authorization": "Bearer mock-token"},
        )
        listing = client.get(
            "/api/security/admin/evaluation/jobs",
            headers={"Authorization": "Bearer mock-token"},
        )
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 2
    # 최신 dataset(tenant_security)이 먼저
    assert items[0]["dataset_name"] == "tenant_security"
    assert items[1]["dataset_name"] == "platform_smoke"


def test_promote_fails_when_gate_did_not_pass():
    """tenant_security gate는 citation_accuracy>=0.90 — mock LLM이 extra citation 때문에
    실패해야 정상. promote 호출 시 409 promotion_gate_not_passed."""
    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/evaluation/run",
            json={"dataset_name": "tenant_security"},
            headers={"Authorization": "Bearer mock-token"},
        )
        job_id = resp.json()["job_id"]

        promote = client.post(
            f"/api/security/admin/evaluation/jobs/{job_id}/promote",
            json={"target": "prompt", "version": "v2"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert promote.status_code == 409
    assert promote.json()["detail"]["error"] == "promotion_gate_not_passed"


def test_promote_succeeds_when_gate_passed_after_override():
    """gate를 통과하는 환경(예: platform_smoke — gate 없음)에서는 promote가 거부되지 않는다.

    platform_smoke은 promotion_gate.yaml이 없으므로 gate_result는 빈 dict({}). orchestrator
    의 'gate_passed' 판정은 False여서 promote 거부 — gate가 있는 dataset이 필요하다.
    대신 tenant_security gate를 통과하도록 citation_accuracy bound를 낮추는 config_override는
    불가(gate는 yaml에서 로드되며 runner와는 다른 경로). 실제 운영 흐름과 정합되도록, 본
    테스트는 gate yaml이 있고 통과하는 시나리오를 만들기 위해 InMemory repo에 직접 record
    를 주입한다.
    """
    import asyncio
    from datetime import datetime, timezone

    from app.core.config import get_settings
    from app.deps import get_evaluation_orchestrator
    from rag_core.interfaces.evaluation_job_repository import EvaluationJobRecord

    orch = get_evaluation_orchestrator(get_settings())
    asyncio.new_event_loop().run_until_complete(
        orch.repo.create(
            EvaluationJobRecord(
                job_id="EVAL-PASS-001",
                domain_id="security",
                dataset_name="tenant_security",
                status="completed",
                actor="seed",
                summary={"total_cases": 5, "retrieval_recall_at_5": 1.0},
                gate_result={"passed": True, "metrics": []},
            )
        )
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/security/admin/evaluation/jobs/EVAL-PASS-001/promote",
            json={"target": "model", "version": "qwen3-7b-v2"},
            headers={"Authorization": "Bearer mock-token"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "promoted"
    assert body["promotion_target"] == "model"
    assert body["promotion_version"] == "qwen3-7b-v2"
    assert body["promoted_by"] == "dev-user-001"
