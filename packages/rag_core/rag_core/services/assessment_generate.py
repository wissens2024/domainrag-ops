"""AssessmentGenerateService — ADR-014 §3 Mode 2.

흐름:
  1. References 수집 — same subject·chapter + quality_status='approved' 후보 top-k
  2. LLM 호출 (shared_llm) — references를 prompt에 주고 신규 item count개 생성
     output schema: { items: [{question_text, choices, answer, explanation, difficulty, ...}] }
  3. 각 신규 item:
     - similarity_check(question_text, candidates=same-subject approved)
     - duplicate(>= duplicate_threshold) → reject + retry up to 3회
     - similar(>= similar_threshold) → reference로 표기
  4. validator pipeline (§5)
  5. repository.upsert(quality_status=draft|reviewed, source='generated', reference_item_ids 채움)
  6. citation 산출
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
    AssessmentItemRepository,
)
from rag_core.interfaces.llm_client import LLMClient
from rag_core.services.assessment_extract import _item_to_citation
from rag_core.services.assessment_similarity import (
    AssessmentSimilarityChecker,
    SimilarityThresholds,
)
from rag_core.services.assessment_validator import (
    AssessmentValidator,
    ValidationOutcome,
)


@dataclass
class GenerateCriteria:
    subject: str
    chapter: str | None = None
    difficulty: str = "medium"
    count: int = 5
    question_type: str = "multiple_choice"


@dataclass
class GenerateResult:
    items: list[AssessmentItemRecord] = field(default_factory=list)
    generated_count: int = 0
    citations: list[dict[str, Any]] = field(default_factory=list)
    validator_summary: dict[str, Any] = field(default_factory=dict)
    similarity_results: list[dict[str, Any]] = field(default_factory=list)
    rejected_duplicates: int = 0
    retries_used: int = 0


_GENERATE_PROMPT = (
    "당신은 시험 문제 출제 전문가입니다. 다음 조건에 맞춰 신규 문제 {count}개를 생성하세요.\n"
    "주제: {subject}\n장: {chapter}\n난이도: {difficulty}\n문제유형: {question_type}\n\n"
    "참고 문제:\n{references}\n\n"
    "응답은 반드시 JSON: "
    '{{"items":[{{"question_text":str,"choices":[str],"answer":str,"explanation":str,"difficulty":"easy|medium|hard","tags":[str]}}]}}'
)


class AssessmentGenerateService:
    def __init__(
        self,
        *,
        repository: AssessmentItemRepository,
        llm_client: LLMClient,
        similarity_checker: AssessmentSimilarityChecker,
        validator: AssessmentValidator,
        model: str = "shared_llm",
        max_retries: int = 3,
        item_index: Any = None,
    ) -> None:
        self._repo = repository
        self._llm = llm_client
        self._similarity = similarity_checker
        self._validator = validator
        self._model = model
        self._max_retries = max_retries
        # ADR-025 §5 — AssessmentItemIndex 주입 시 dedup을 사전 인덱스 검색으로 전환
        # (후보 전체 재임베딩 제거). None이면 기존 on-the-fly similarity 유지.
        self._item_index = item_index

    async def generate(
        self,
        *,
        domain_id: str,
        criteria: GenerateCriteria,
        actor: str | None = None,
        validators_config: dict[str, dict[str, Any]] | None = None,
        persist: bool = True,
    ) -> GenerateResult:
        """ADR-014 §3 Mode 2 출제.

        persist=False(ADR-027 §6): 대화형 채팅 출제용. 생성 문항을 DB에
        upsert하지 않고 result.items로만 반환한다(ephemeral). 채점은 호출자가
        반환된 정답·해설을 신뢰원으로 쓴다. 콘솔 출제는 persist=True(기본) 유지.
        """
        result = GenerateResult()

        # 1. references 수집
        from rag_core.interfaces.assessment_item_repository import ExtractCriteria

        refs = await self._repo.list_candidates_for_extract(
            domain_id=domain_id,
            criteria=ExtractCriteria(
                subject=criteria.subject,
                chapter=criteria.chapter,
                quality_status=["approved"],
            ),
            limit=10,
        )
        # 인덱스 소비 시 후보 전체 재임베딩이 불필요(사전 인덱스 검색으로 dedup).
        refs_for_similarity: list[AssessmentItemRecord] = []
        if self._item_index is None:
            refs_for_similarity = await self._repo.list_candidates_for_extract(
                domain_id=domain_id,
                criteria=ExtractCriteria(
                    subject=criteria.subject,
                    quality_status=["approved", "reviewed"],
                ),
                limit=500,
            )

        # 2. LLM 호출 + 3. similarity + 4. validator
        target = criteria.count
        produced: list[AssessmentItemRecord] = []
        validator_scores: list[float] = []

        for retry in range(self._max_retries):
            needed = target - len(produced)
            if needed <= 0:
                break
            result.retries_used = retry + 1
            prompt = _GENERATE_PROMPT.format(
                count=needed,
                subject=criteria.subject,
                chapter=criteria.chapter or "",
                difficulty=criteria.difficulty,
                question_type=criteria.question_type,
                references="\n".join(
                    f"- [{r.item_id}] {r.question_text} (정답: {r.answer})"
                    for r in refs[:5]
                ) or "(참고 가능한 기존 문제 없음)",
            )
            try:
                raw = await self._llm.generate(
                    prompt, model=self._model,
                    max_tokens=2048, temperature=0.4,
                )
            except Exception:  # noqa: BLE001
                continue
            parsed, err = _parse_json_items(raw)
            if err:
                continue

            for candidate in parsed:
                if len(produced) >= target:
                    break
                qtext = str(candidate.get("question_text", "")).strip()
                if not qtext:
                    continue
                # similarity check — 인덱스가 있으면 사전 인덱스 검색(후보 재임베딩 제거),
                # 없으면 기존 on-the-fly 임베딩 비교.
                if self._item_index is not None:
                    hits = await self._item_index.search_similar(
                        domain_id=domain_id, question_text=qtext,
                        subject=criteria.subject, top_k=10,
                    )
                    thr = self._similarity.thresholds
                    max_similarity = hits[0]["score"] if hits else 0.0
                    is_duplicate = max_similarity >= thr.duplicate
                    similar_candidates = [
                        {"item_id": h["item_id"], "similarity": round(h["score"], 4)}
                        for h in hits if h["score"] >= thr.similar
                    ]
                else:
                    sim = await self._similarity.check(
                        new_question_text=qtext, candidates=refs_for_similarity,
                    )
                    max_similarity = sim.max_similarity
                    is_duplicate = sim.is_duplicate
                    similar_candidates = sim.similar_candidates
                result.similarity_results.append({
                    "question_text_excerpt": qtext[:80],
                    "max_similarity": max_similarity,
                    "is_duplicate": is_duplicate,
                    "similar_candidates": similar_candidates,
                })
                if is_duplicate:
                    result.rejected_duplicates += 1
                    continue

                # validator
                outcome: ValidationOutcome = await self._validator.validate(
                    question_text=qtext,
                    choices=list(candidate.get("choices") or []),
                    answer=str(candidate.get("answer", "")),
                    explanation=candidate.get("explanation"),
                    difficulty=str(candidate.get("difficulty") or criteria.difficulty),
                    validators_config=validators_config,
                )
                validator_scores.append(outcome.quality_score)

                # upsert
                item_id = f"Q-{uuid.uuid4().hex[:12]}"
                reference_item_ids = [s["item_id"] for s in similar_candidates]
                record = AssessmentItemRecord(
                    item_id=item_id,
                    domain_id=domain_id,
                    subject=criteria.subject,
                    chapter=criteria.chapter,
                    difficulty=str(candidate.get("difficulty") or criteria.difficulty),
                    question_type=criteria.question_type,
                    question_text=qtext,
                    choices=list(candidate.get("choices") or []),
                    answer=str(candidate.get("answer", "")),
                    explanation=str(candidate.get("explanation", "")) or None,
                    tags=list(candidate.get("tags") or []),
                    quality_status=outcome.quality_status,
                    quality_score=outcome.quality_score,
                    validator_results=outcome.validator_results,
                    source="generated",
                    reference_item_ids=reference_item_ids,
                )
                # ADR-027 §6 — persist=False(채팅 출제)면 DB 저장 생략(ephemeral).
                if persist:
                    await self._repo.upsert(record)
                produced.append(record)

        result.items = produced
        result.generated_count = len(produced)
        if validator_scores:
            result.validator_summary = {
                "avg_quality_score": round(
                    sum(validator_scores) / len(validator_scores), 4
                ),
                "min_quality_score": round(min(validator_scores), 4),
                "max_quality_score": round(max(validator_scores), 4),
            }
        for i, item in enumerate(result.items, start=1):
            result.citations.append(_item_to_citation(item, marker=f"[{i}]", domain_id=domain_id))
        return result


def _parse_json_items(raw: str) -> tuple[list[dict], str | None]:
    if not raw:
        return [], "empty"
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        data = json.loads(s)
    except Exception as exc:  # noqa: BLE001
        return [], f"json_parse_error: {exc}"
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], "items_not_list"
    return items, None
