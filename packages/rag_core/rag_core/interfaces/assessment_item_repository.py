"""AssessmentItemRepository — ADR-014 + ADR-017 §17 item CRUD Protocol.

assessment_items 테이블 1:1 매핑. RLS는 구현체 책임. similarity check를 위한
candidate 조회·used_count 갱신·quality_status 전이를 지원.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class AssessmentItemRecord:
    """assessment_items row 1건."""

    item_id: str
    domain_id: str
    subject: str | None = None
    chapter: str | None = None
    difficulty: str | None = None  # easy | medium | hard
    question_type: str | None = None  # multiple_choice | true_false | short_answer | essay
    question_text: str = ""
    choices: list[Any] = field(default_factory=list)
    answer: str = ""
    explanation: str | None = None
    tags: list[str] = field(default_factory=list)
    quality_status: str = "draft"  # draft | reviewed | approved | retired
    quality_score: float | None = None
    validator_results: dict[str, Any] = field(default_factory=dict)
    used_count: int = 0
    last_used_at: datetime | None = None
    source: str | None = None  # 'imported' | 'generated' | 'hybrid'
    reference_item_ids: list[str] = field(default_factory=list)
    # ADR-025 — 멀티모달 자산(이미지). assets[i]: {asset_id, kind, storage_key,
    # content_hash, source_page, bbox, vlm_description}. figure_dependent=그림 없이 못 푸는 문항.
    assets: list[dict[str, Any]] = field(default_factory=list)
    figure_dependent: bool = False
    embedding_model: str | None = None
    vector_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class ExtractCriteria:
    """SQL 조건 매칭 기준."""

    subject: str | None = None
    chapter: str | None = None
    difficulty: str | None = None  # 단일 — distribution은 caller가 mode마다 분기
    difficulty_distribution: dict[str, int] | None = None  # {easy:3, medium:5, hard:2}
    quality_status: list[str] = field(default_factory=lambda: ["approved"])
    exclude_recent_days: int | None = None
    tags_any: list[str] = field(default_factory=list)


class AssessmentItemConflictError(Exception):
    def __init__(self, item_id: str) -> None:
        super().__init__(f"assessment item already exists: {item_id}")
        self.item_id = item_id


class AssessmentItemRepository(Protocol):
    async def get(
        self, *, domain_id: str, item_id: str
    ) -> AssessmentItemRecord | None: ...

    async def upsert(self, record: AssessmentItemRecord) -> AssessmentItemRecord:
        """item_id UNIQUE 충돌 시 AssessmentItemConflictError."""
        ...

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        keyword: str | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        difficulty: str | None = None,
        quality_status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AssessmentItemRecord], int]:
        """admin 목록. total 함께 반환."""
        ...

    async def list_candidates_for_extract(
        self, *, domain_id: str, criteria: ExtractCriteria, limit: int = 200
    ) -> list[AssessmentItemRecord]: ...

    async def update_quality(
        self,
        *,
        domain_id: str,
        item_id: str,
        quality_status: str | None = None,
        quality_score: float | None = None,
        validator_results: dict[str, Any] | None = None,
    ) -> AssessmentItemRecord | None: ...

    async def touch_used(
        self, *, domain_id: str, item_ids: list[str]
    ) -> int:
        """used_count += 1, last_used_at = NOW(). 영향 row 수 반환."""
        ...

    async def list_review_queue(
        self,
        *,
        domain_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AssessmentItemRecord], int]:
        """quality_status='draft' (또는 'reviewed') 검토 대기 목록."""
        ...

    async def analytics_summary(
        self, *, domain_id: str
    ) -> dict[str, Any]:
        """subject/chapter/difficulty별 used_count + status 분포."""
        ...

    async def bulk_approve(
        self,
        *,
        domain_id: str,
        subject: str | None = None,
        chapter: str | None = None,
        difficulty: str | None = None,
        keyword: str | None = None,
    ) -> int:
        """draft/reviewed 매칭 item을 일괄 approved 전이. 전이된 수 반환 (ADR-014 §5·Y2)."""
        ...


# --------------------------------------------------------------------------- #
# InMemory — dev / tests
# --------------------------------------------------------------------------- #


class InMemoryAssessmentItemRepository:
    def __init__(self) -> None:
        # (domain_id, item_id) → record
        self._records: dict[tuple[str, str], AssessmentItemRecord] = {}

    async def get(
        self, *, domain_id: str, item_id: str
    ) -> AssessmentItemRecord | None:
        return self._records.get((domain_id, item_id))

    async def upsert(
        self, record: AssessmentItemRecord
    ) -> AssessmentItemRecord:
        key = (record.domain_id, record.item_id)
        if key in self._records and self._records[key].created_at is not None:
            # update 모드 — created_at 보존
            existing = self._records[key]
            record.created_at = existing.created_at
        else:
            record.created_at = record.created_at or datetime.utcnow()
        record.updated_at = datetime.utcnow()
        self._records[key] = record
        return record

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        keyword: str | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        difficulty: str | None = None,
        quality_status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AssessmentItemRecord], int]:
        rows = [r for (tid, _iid), r in self._records.items() if tid == domain_id]
        if subject is not None:
            rows = [r for r in rows if r.subject == subject]
        if chapter is not None:
            rows = [r for r in rows if r.chapter == chapter]
        if difficulty is not None:
            rows = [r for r in rows if r.difficulty == difficulty]
        if quality_status is not None:
            rows = [r for r in rows if r.quality_status == quality_status]
        if keyword:
            needle = keyword.lower()
            rows = [
                r for r in rows
                if needle in (r.question_text or "").lower()
                or needle in (r.item_id or "").lower()
            ]
        total = len(rows)
        rows.sort(key=lambda r: r.updated_at or datetime.min, reverse=True)
        start = (page - 1) * page_size
        return rows[start : start + page_size], total

    async def list_candidates_for_extract(
        self, *, domain_id: str, criteria: ExtractCriteria, limit: int = 200
    ) -> list[AssessmentItemRecord]:
        rows = [r for (tid, _iid), r in self._records.items() if tid == domain_id]
        if criteria.subject is not None:
            rows = [r for r in rows if r.subject == criteria.subject]
        if criteria.chapter is not None:
            rows = [r for r in rows if r.chapter == criteria.chapter]
        if criteria.difficulty is not None:
            rows = [r for r in rows if r.difficulty == criteria.difficulty]
        if criteria.quality_status:
            allowed = set(criteria.quality_status)
            rows = [r for r in rows if r.quality_status in allowed]
        if criteria.exclude_recent_days is not None:
            threshold = datetime.utcnow().timestamp() - criteria.exclude_recent_days * 86400
            rows = [
                r for r in rows
                if r.last_used_at is None or r.last_used_at.timestamp() < threshold
            ]
        if criteria.tags_any:
            wanted = set(criteria.tags_any)
            rows = [r for r in rows if wanted & set(r.tags or [])]
        return rows[:limit]

    async def update_quality(
        self,
        *,
        domain_id: str,
        item_id: str,
        quality_status: str | None = None,
        quality_score: float | None = None,
        validator_results: dict[str, Any] | None = None,
    ) -> AssessmentItemRecord | None:
        rec = self._records.get((domain_id, item_id))
        if rec is None:
            return None
        if quality_status is not None:
            rec.quality_status = quality_status
        if quality_score is not None:
            rec.quality_score = quality_score
        if validator_results is not None:
            rec.validator_results = dict(validator_results)
        rec.updated_at = datetime.utcnow()
        return rec

    async def touch_used(
        self, *, domain_id: str, item_ids: list[str]
    ) -> int:
        now = datetime.utcnow()
        affected = 0
        for iid in item_ids:
            rec = self._records.get((domain_id, iid))
            if rec is None:
                continue
            rec.used_count += 1
            rec.last_used_at = now
            affected += 1
        return affected

    async def bulk_approve(
        self,
        *,
        domain_id: str,
        subject: str | None = None,
        chapter: str | None = None,
        difficulty: str | None = None,
        keyword: str | None = None,
    ) -> int:
        needle = keyword.lower() if keyword else None
        count = 0
        for (tid, _iid), r in self._records.items():
            if tid != domain_id or r.quality_status not in {"draft", "reviewed"}:
                continue
            if subject is not None and r.subject != subject:
                continue
            if chapter is not None and r.chapter != chapter:
                continue
            if difficulty is not None and r.difficulty != difficulty:
                continue
            if needle and needle not in (r.question_text or "").lower() and needle not in (
                r.item_id or ""
            ).lower():
                continue
            r.quality_status = "approved"
            r.updated_at = datetime.utcnow()
            count += 1
        return count

    async def list_review_queue(
        self,
        *,
        domain_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AssessmentItemRecord], int]:
        rows = [
            r for (tid, _iid), r in self._records.items()
            if tid == domain_id and r.quality_status in {"draft", "reviewed"}
        ]
        total = len(rows)
        rows.sort(key=lambda r: r.created_at or datetime.min)  # 오래된 것부터 검토
        start = (page - 1) * page_size
        return rows[start : start + page_size], total

    async def analytics_summary(
        self, *, domain_id: str
    ) -> dict[str, Any]:
        rows = [r for (tid, _iid), r in self._records.items() if tid == domain_id]
        by_status: dict[str, int] = {}
        by_difficulty: dict[str, int] = {}
        by_subject: dict[str, int] = {}
        unused = 0
        for r in rows:
            by_status[r.quality_status] = by_status.get(r.quality_status, 0) + 1
            if r.difficulty:
                by_difficulty[r.difficulty] = by_difficulty.get(r.difficulty, 0) + 1
            if r.subject:
                by_subject[r.subject] = by_subject.get(r.subject, 0) + 1
            if r.used_count == 0:
                unused += 1
        return {
            "total_items": len(rows),
            "by_quality_status": by_status,
            "by_difficulty": by_difficulty,
            "by_subject": by_subject,
            "unused_count": unused,
        }
