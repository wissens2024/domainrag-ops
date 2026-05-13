"""AssessmentHybridService — ADR-014 §3 Mode 3.

extract + generate를 같은 요청에서 조합. 출력 items 통합 + citation marker 통합.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
    ExtractCriteria,
)
from rag_core.services.assessment_extract import (
    AssessmentExtractService,
    _item_to_citation,
)
from rag_core.services.assessment_generate import (
    AssessmentGenerateService,
    GenerateCriteria,
)


@dataclass
class HybridResult:
    items: list[AssessmentItemRecord] = field(default_factory=list)
    extracted_count: int = 0
    generated_count: int = 0
    citations: list[dict[str, Any]] = field(default_factory=list)
    insufficient_pool: dict[str, int] = field(default_factory=dict)
    rejected_duplicates: int = 0
    validator_summary: dict[str, Any] = field(default_factory=dict)
    similarity_results: list[dict[str, Any]] = field(default_factory=list)


class AssessmentHybridService:
    def __init__(
        self,
        *,
        extract_service: AssessmentExtractService,
        generate_service: AssessmentGenerateService,
    ) -> None:
        self._extract = extract_service
        self._generate = generate_service

    async def run(
        self,
        *,
        tenant_id: str,
        extract_criteria: ExtractCriteria,
        generate_criteria: GenerateCriteria,
        validators_config: dict[str, dict[str, Any]] | None = None,
        actor: str | None = None,
    ) -> HybridResult:
        ex_result = await self._extract.extract(
            tenant_id=tenant_id, criteria=extract_criteria,
        )
        gen_result = await self._generate.generate(
            tenant_id=tenant_id,
            criteria=generate_criteria,
            actor=actor,
            validators_config=validators_config,
        )
        result = HybridResult()
        result.items = list(ex_result.items) + list(gen_result.items)
        result.extracted_count = ex_result.extracted_count
        result.generated_count = gen_result.generated_count
        result.insufficient_pool = dict(ex_result.insufficient_pool)
        result.rejected_duplicates = gen_result.rejected_duplicates
        result.validator_summary = dict(gen_result.validator_summary)
        result.similarity_results = list(gen_result.similarity_results)
        # citation marker 통합 — 1..N
        for i, item in enumerate(result.items, start=1):
            result.citations.append(
                _item_to_citation(item, marker=f"[{i}]", tenant_id=tenant_id)
            )
        return result
