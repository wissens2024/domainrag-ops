"""InMemoryAssessmentItemRepository unit tests — ADR-014 §1."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
    ExtractCriteria,
    InMemoryAssessmentItemRepository,
)


def _item(item_id: str, **kwargs) -> AssessmentItemRecord:
    base = dict(
        item_id=item_id, tenant_id=kwargs.pop("tenant_id", "t1"),
        subject=kwargs.pop("subject", "정보보안"),
        chapter=kwargs.pop("chapter", "접근통제"),
        difficulty=kwargs.pop("difficulty", "medium"),
        question_type="multiple_choice",
        question_text=kwargs.pop("question_text", "샘플 문제"),
        choices=kwargs.pop("choices", ["A", "B", "C", "D"]),
        answer=kwargs.pop("answer", "A"),
        quality_status=kwargs.pop("quality_status", "approved"),
    )
    base.update(kwargs)
    return AssessmentItemRecord(**base)


async def test_upsert_and_get():
    repo = InMemoryAssessmentItemRepository()
    rec = await repo.upsert(_item("Q-1"))
    assert rec.created_at is not None
    got = await repo.get(tenant_id="t1", item_id="Q-1")
    assert got is not None
    assert got.question_text == "샘플 문제"


async def test_list_by_tenant_keyword_and_filters():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-1", question_text="패스워드 정책"))
    await repo.upsert(_item("Q-2", question_text="VPN 구성", subject="네트워크"))
    items, total = await repo.list_by_tenant(
        tenant_id="t1", keyword="패스워드",
    )
    assert total == 1
    assert items[0].item_id == "Q-1"

    items, total = await repo.list_by_tenant(
        tenant_id="t1", subject="네트워크",
    )
    assert total == 1
    assert items[0].item_id == "Q-2"


async def test_extract_candidates_filters_status_and_recent_usage():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-A", quality_status="approved"))
    await repo.upsert(_item("Q-D", quality_status="draft"))
    # Q-A used 1 day ago
    rec = await repo.upsert(_item("Q-RECENT", quality_status="approved"))
    rec.last_used_at = datetime.utcnow() - timedelta(days=1)

    candidates = await repo.list_candidates_for_extract(
        tenant_id="t1",
        criteria=ExtractCriteria(quality_status=["approved"]),
    )
    ids = {c.item_id for c in candidates}
    assert "Q-A" in ids
    assert "Q-RECENT" in ids
    assert "Q-D" not in ids  # 'draft' 제외

    # exclude_recent_days=7 — Q-RECENT는 1일 전 사용 → 제외
    candidates2 = await repo.list_candidates_for_extract(
        tenant_id="t1",
        criteria=ExtractCriteria(
            quality_status=["approved"], exclude_recent_days=7
        ),
    )
    ids2 = {c.item_id for c in candidates2}
    assert "Q-A" in ids2
    assert "Q-RECENT" not in ids2


async def test_touch_used_increments_and_sets_last_used():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-1"))
    await repo.upsert(_item("Q-2"))
    affected = await repo.touch_used(tenant_id="t1", item_ids=["Q-1", "Q-2", "ghost"])
    assert affected == 2
    rec = await repo.get(tenant_id="t1", item_id="Q-1")
    assert rec.used_count == 1
    assert rec.last_used_at is not None


async def test_update_quality_changes_status_and_score():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-1", quality_status="draft"))
    updated = await repo.update_quality(
        tenant_id="t1", item_id="Q-1",
        quality_status="approved", quality_score=0.92,
    )
    assert updated.quality_status == "approved"
    assert updated.quality_score == 0.92


async def test_review_queue_returns_draft_and_reviewed():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-D", quality_status="draft"))
    await repo.upsert(_item("Q-R", quality_status="reviewed"))
    await repo.upsert(_item("Q-A", quality_status="approved"))
    items, total = await repo.list_review_queue(tenant_id="t1")
    ids = {r.item_id for r in items}
    assert ids == {"Q-D", "Q-R"}
    assert total == 2


async def test_analytics_summary_aggregates():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-1", difficulty="easy", quality_status="approved"))
    await repo.upsert(_item("Q-2", difficulty="medium", quality_status="approved"))
    await repo.upsert(_item("Q-3", difficulty="medium", quality_status="draft"))
    summary = await repo.analytics_summary(tenant_id="t1")
    assert summary["total_items"] == 3
    assert summary["by_difficulty"]["medium"] == 2
    assert summary["by_quality_status"]["approved"] == 2
    assert summary["by_quality_status"]["draft"] == 1
    assert summary["unused_count"] == 3


async def test_tenant_isolation():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-1", tenant_id="t1"))
    await repo.upsert(_item("Q-1", tenant_id="t2"))  # 다른 tenant 같은 item_id
    t1_item = await repo.get(tenant_id="t1", item_id="Q-1")
    t2_item = await repo.get(tenant_id="t2", item_id="Q-1")
    assert t1_item is not None and t2_item is not None
    assert t1_item.tenant_id == "t1"
    assert t2_item.tenant_id == "t2"
