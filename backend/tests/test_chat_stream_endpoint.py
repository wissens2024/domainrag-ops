"""POST /api/{tenant_id}/chat/stream — SSE chat_streaming endpoint 통합 테스트.

env 고정: AUTH_MODE=mock + RAG_BACKEND=inmemory (conftest.py).
inmemory LLM은 stream_chunks=['mock ', 'answer'] 기본값 사용.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _parse_sse(body: str) -> list[tuple[str, str]]:
    """text/event-stream을 [(event, data), ...]로 파싱."""
    out: list[tuple[str, str]] = []
    current_event: str | None = None
    current_data: list[str] = []
    for line in body.splitlines():
        if not line:
            if current_event and current_data:
                out.append((current_event, "\n".join(current_data)))
            current_event, current_data = None, []
            continue
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current_data.append(line[len("data:"):].lstrip())
    if current_event and current_data:
        out.append((current_event, "\n".join(current_data)))
    return out


def test_chat_stream_returns_sse_tokens_then_complete():
    from app.deps import reset_rag_service

    reset_rag_service()
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/security/chat/stream",
            json={
                "conversation_id": None,
                "question": "안녕하세요",
            },
            headers={"Authorization": "Bearer mock-token"},
        ) as resp:
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    # 최소 1 token + 1 complete
    assert any(e == "token" for e, _ in events)
    assert events[-1][0] == "complete"
    # complete payload는 JSON
    import json as _json
    final = _json.loads(events[-1][1])
    assert final["metadata"]["ui_mode"] == "chat_streaming"
    assert final["metadata"]["citation_disabled"] is True
    assert final["message_id"]


def test_chat_stream_blocks_input_pii():
    from app.deps import reset_rag_service

    reset_rag_service()
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/security/chat/stream",
            json={
                "conversation_id": None,
                "question": "제 주민번호 901231-1234567 로 확인해 주세요",
            },
            headers={"Authorization": "Bearer mock-token"},
        ) as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes()).decode("utf-8")

    events = _parse_sse(body)
    # 단일 fallback event — token 없음
    kinds = [e for e, _ in events]
    assert "token" not in kinds
    assert events[-1][0] == "fallback"
    import json as _json
    payload = _json.loads(events[-1][1])
    assert payload["reason"] == "input_pii_blocked"
    assert "rrn" in payload["blocked_categories"]


# NOTE: tenant mismatch 403은 운영(AuthFusionAdapter) 환경에서만 검증 가능.
# Mock adapter는 모든 tenant_id를 허용한다 (auth_adapter.py MockAuthAdapter).
