"""DashboardAnalytics — admin 대시보드 집계 단일 진입점 Protocol.

ADR-017 §10 응답 schema에 1:1 매핑. tenant scope (RLS 또는 dict 필터)이고,
"today"는 운영 timezone KST(Asia/Seoul, Y4) 자정 기준. 운영 구현은 Postgres
COUNT/AVG/GROUP BY SQL, dev/test는 InMemory state 위에서 동일 집계.

routing_distribution key는 ADR-017 §10 예시 (`tenant_slm_lora` / `tenant_slm_no_lora`
/ `shared_llm`)를 따라 `selected_model + ('_lora' if selected_lora else '_no_lora')`
조합으로 정한다. shared_llm은 lora 사용 안 함 — 단일 key.

negative_feedback_rate는 chat_logs.feedback='bad' / today total. Feedback API가
501 stub인 동안엔 InMemory에서 0.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Protocol


# Asia/Seoul (Y4) — UTC+9 fixed offset (DST 없음)
KST: tzinfo = timezone(timedelta(hours=9))


@dataclass
class DashboardSnapshot:
    """ADR-017 §10 응답 — 12개 메트릭."""

    total_documents: int = 0
    total_chunks: int = 0
    uploaded_today: int = 0
    indexing_completed_today: int = 0
    indexing_failed_today: int = 0
    questions_today: int = 0
    avg_latency_ms: float = 0.0
    answers_without_citation: int = 0
    negative_feedback_rate: float = 0.0
    citation_type_distribution: dict[str, int] = field(default_factory=dict)
    fallback_distribution: dict[str, int] = field(default_factory=dict)
    routing_distribution: dict[str, int] = field(default_factory=dict)


class DashboardAnalytics(Protocol):
    async def get_snapshot(self, *, domain_id: str) -> DashboardSnapshot: ...


# --------------------------------------------------------------------------- #
# InMemory variant — dev / tests
# --------------------------------------------------------------------------- #


class InMemoryDashboardAnalytics:
    """InMemory store 위에서 dashboard 집계.

    Args:
        chat_log_writer: InMemoryChatLogWriter — questions/citations/routing 집계
        document_repo: InMemoryDocumentRepository — total_documents/uploaded_today
        chunk_repo: InMemoryChunkRepository — total_chunks
        indexing_job_repo: InMemoryIndexingJobRepository — indexing completed/failed
        clock: today 기준 시각 (default: datetime.now(KST))

    InMemory store는 created_at을 추적하지 않으므로 모든 row를 "today"로 간주한다.
    즉 uploaded_today == total_documents, indexing_*_today == total active jobs by status.
    운영(Postgres)에서는 SQL created_at >= KST today로 정확 분리.
    """

    def __init__(
        self,
        *,
        chat_log_writer,
        document_repo,
        chunk_repo,
        indexing_job_repo,
        clock=lambda: datetime.now(KST),
    ) -> None:
        self._writer = chat_log_writer
        self._docs = document_repo
        self._chunks = chunk_repo
        self._jobs = indexing_job_repo
        self._clock = clock

    async def get_snapshot(self, *, domain_id: str) -> DashboardSnapshot:
        snap = DashboardSnapshot()

        # documents
        doc_records = [
            d for (tid, _did, _ver), d in self._docs._docs.items() if tid == domain_id
        ]
        snap.total_documents = len(doc_records)
        snap.uploaded_today = len(doc_records)  # InMemory: 모두 today

        # chunks (archived 제외 — retrieval default 정합)
        chunk_total = 0
        for (tid, _did, _ver, _pv), chunks in self._chunks._chunks.items():
            if tid != domain_id:
                continue
            chunk_total += sum(
                1 for c in chunks if not getattr(c, "archived_at", None)
            )
        snap.total_chunks = chunk_total

        # indexing jobs
        for j in self._jobs._jobs.values():
            if j.domain_id != domain_id:
                continue
            if j.status == "completed":
                snap.indexing_completed_today += 1
            elif j.status == "failed":
                snap.indexing_failed_today += 1

        # chat_logs aggregate
        chat_records = [
            r for r in self._writer.records if r.domain_id == domain_id
        ]
        snap.questions_today = len(chat_records)
        if chat_records:
            latencies = [r.latency_ms for r in chat_records if r.latency_ms]
            snap.avg_latency_ms = (
                sum(latencies) / len(latencies) if latencies else 0.0
            )
            snap.answers_without_citation = sum(
                1 for r in chat_records if not r.citations
            )
            # citation_type_distribution: 같은 row의 같은 type은 1번만 카운트
            for r in chat_records:
                for ct in set(r.citation_types or []):
                    snap.citation_type_distribution[ct] = (
                        snap.citation_type_distribution.get(ct, 0) + 1
                    )
            for r in chat_records:
                if r.fallback_reason:
                    snap.fallback_distribution[r.fallback_reason] = (
                        snap.fallback_distribution.get(r.fallback_reason, 0) + 1
                    )
            for r in chat_records:
                key = _routing_distribution_key(r.routing_decision)
                if key:
                    snap.routing_distribution[key] = (
                        snap.routing_distribution.get(key, 0) + 1
                    )
            # negative_feedback_rate — ChatLogPayload.feedback이 'bad'인 비율
            bad = sum(1 for r in chat_records if getattr(r, "feedback", None) == "bad")
            snap.negative_feedback_rate = bad / len(chat_records)

        return snap


def _routing_distribution_key(routing_decision: dict[str, Any] | None) -> str | None:
    """routing_decision → distribution key.

    `{selected_model, selected_lora}` 조합. selected_model이 없으면 None.
    shared_llm은 lora 사용 안 함 → 항상 단일 key 'shared_llm'.
    """
    if not routing_decision:
        return None
    model = routing_decision.get("selected_model")
    if not model:
        return None
    lora = routing_decision.get("selected_lora")
    if model == "shared_llm":
        return "shared_llm"
    return f"{model}_lora" if lora else f"{model}_no_lora"


def _kst_today_start(now: datetime | None = None) -> datetime:
    """KST 자정. now가 timezone-naive면 UTC로 가정."""
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    kst_now = now.astimezone(KST)
    return datetime(
        kst_now.year, kst_now.month, kst_now.day, tzinfo=KST
    )
