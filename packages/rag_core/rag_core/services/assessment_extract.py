"""AssessmentExtractService — ADR-014 §3 Mode 1.

SQL 조건 매칭 + 난이도 분포 sampling + used_count 증가.

흐름:
  1. repository.list_candidates_for_extract(criteria) — quality_status filter + exclude_recent_days
  2. difficulty_distribution이 주어지면 각 난이도별 random sampling
  3. repository.touch_used(selected) — used_count + last_used_at 갱신
  4. result + citation 산출
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
    AssessmentItemRepository,
    ExtractCriteria,
)


@dataclass
class ExtractResult:
    items: list[AssessmentItemRecord] = field(default_factory=list)
    extracted_count: int = 0
    citations: list[dict[str, Any]] = field(default_factory=list)
    insufficient_pool: dict[str, int] = field(default_factory=dict)
    # insufficient_pool[difficulty] = 부족분(요청 - 가용)


class AssessmentExtractService:
    def __init__(
        self,
        *,
        repository: AssessmentItemRepository,
        rng: random.Random | None = None,
    ) -> None:
        self._repo = repository
        self._rng = rng or random.Random()

    async def extract(
        self,
        *,
        tenant_id: str,
        criteria: ExtractCriteria,
    ) -> ExtractResult:
        candidates = await self._repo.list_candidates_for_extract(
            tenant_id=tenant_id, criteria=criteria, limit=1000,
        )
        result = ExtractResult()

        if criteria.difficulty_distribution:
            by_difficulty: dict[str, list[AssessmentItemRecord]] = {}
            for c in candidates:
                key = c.difficulty or "unknown"
                by_difficulty.setdefault(key, []).append(c)
            selected: list[AssessmentItemRecord] = []
            for diff, requested in criteria.difficulty_distribution.items():
                pool = by_difficulty.get(diff, [])
                if len(pool) < requested:
                    result.insufficient_pool[diff] = requested - len(pool)
                pick = min(len(pool), requested)
                if pool and pick > 0:
                    selected.extend(self._rng.sample(pool, pick))
            result.items = selected
        else:
            # 단일 difficulty 또는 distribution 없음 — 전체 후보에서 순서대로
            result.items = list(candidates)

        result.extracted_count = len(result.items)
        if result.items:
            await self._repo.touch_used(
                tenant_id=tenant_id,
                item_ids=[r.item_id for r in result.items],
            )

        for i, item in enumerate(result.items, start=1):
            result.citations.append(_item_to_citation(item, marker=f"[{i}]", tenant_id=tenant_id))
        return result


def _item_to_citation(
    item: AssessmentItemRecord, *, marker: str, tenant_id: str
) -> dict[str, Any]:
    return {
        "citation_id": f"cite-item-{item.item_id}",
        "marker": marker,
        "tenant_id": tenant_id,
        "support_type": "direct",
        "item_id": item.item_id,
        "subject": item.subject,
        "chapter": item.chapter,
        "difficulty": item.difficulty,
        "question_text_excerpt": (item.question_text or "")[:120],
        "verified": item.quality_status == "approved",
    }
