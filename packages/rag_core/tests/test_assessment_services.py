"""rag_core assessment services unit tests — ADR-014 §3·§5.

extract / similarity / validator / generate를 mock LLM·embedder로 결정론 검증.
"""

from __future__ import annotations

import json
import random

import pytest

from rag_core.clients.in_memory import InMemoryEmbedder
from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
    ExtractCriteria,
    InMemoryAssessmentItemRepository,
)
from rag_core.services.assessment_extract import AssessmentExtractService
from rag_core.services.assessment_generate import (
    AssessmentGenerateService,
    GenerateCriteria,
)
from rag_core.services.assessment_similarity import (
    AssessmentSimilarityChecker,
    SimilarityThresholds,
)
from rag_core.services.assessment_validator import AssessmentValidator


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _item(item_id, **kw):
    base = dict(
        item_id=item_id, domain_id=kw.pop("domain_id", "t1"),
        subject="정보보안", chapter="접근통제",
        difficulty=kw.pop("difficulty", "medium"),
        question_type="multiple_choice",
        question_text=kw.pop("question_text", f"문제 {item_id}"),
        choices=["A", "B", "C", "D"],
        answer="A", quality_status=kw.pop("quality_status", "approved"),
    )
    base.update(kw)
    return AssessmentItemRecord(**base)


class _FakeLLM:
    """generate()가 사전 큐의 응답을 FIFO 반환."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def generate(self, prompt, *, model, max_tokens=1024, temperature=0.2,
                       guided_json_schema=None, lora_adapter=None):
        self.calls.append({"prompt": prompt, "model": model})
        return self._responses.pop(0) if self._responses else "{}"


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #


async def test_extract_distribution_samples_required_difficulties():
    repo = InMemoryAssessmentItemRepository()
    # 풀: easy 5, medium 5, hard 5
    for i in range(5):
        await repo.upsert(_item(f"Q-E{i}", difficulty="easy"))
        await repo.upsert(_item(f"Q-M{i}", difficulty="medium"))
        await repo.upsert(_item(f"Q-H{i}", difficulty="hard"))

    service = AssessmentExtractService(
        repository=repo, rng=random.Random(42),
    )
    result = await service.extract(
        domain_id="t1",
        criteria=ExtractCriteria(
            difficulty_distribution={"easy": 3, "medium": 2, "hard": 1},
        ),
    )
    assert result.extracted_count == 6
    diffs = [i.difficulty for i in result.items]
    assert diffs.count("easy") == 3
    assert diffs.count("medium") == 2
    assert diffs.count("hard") == 1
    # used_count 갱신
    for r in result.items:
        rec = await repo.get(domain_id="t1", item_id=r.item_id)
        assert rec.used_count == 1


async def test_extract_reports_insufficient_pool():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-E1", difficulty="easy"))
    service = AssessmentExtractService(repository=repo)
    result = await service.extract(
        domain_id="t1",
        criteria=ExtractCriteria(
            difficulty_distribution={"easy": 3},
        ),
    )
    assert result.extracted_count == 1
    assert result.insufficient_pool["easy"] == 2


async def test_extract_emits_citations():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-1"))
    service = AssessmentExtractService(repository=repo)
    result = await service.extract(
        domain_id="t1", criteria=ExtractCriteria(),
    )
    assert result.citations[0]["item_id"] == "Q-1"
    assert result.citations[0]["support_type"] == "direct"
    assert result.citations[0]["marker"] == "[1]"


# --------------------------------------------------------------------------- #
# Similarity
# --------------------------------------------------------------------------- #


async def test_similarity_detects_duplicate():
    embedder = InMemoryEmbedder()
    checker = AssessmentSimilarityChecker(
        embedder=embedder,
        thresholds=SimilarityThresholds(duplicate=0.6, similar=0.3),
    )
    cand = _item("Q-1", question_text="패스워드 정책에 대한 설명")
    result = await checker.check(
        new_question_text="패스워드 정책에 대한 설명",
        candidates=[cand],
    )
    assert result.is_duplicate is True
    assert result.max_similarity > 0.9


async def test_similarity_no_candidates_returns_zero():
    embedder = InMemoryEmbedder()
    checker = AssessmentSimilarityChecker(
        embedder=embedder, thresholds=SimilarityThresholds(),
    )
    result = await checker.check(
        new_question_text="x", candidates=[],
    )
    assert result.max_similarity == 0.0
    assert result.is_duplicate is False
    assert result.similar_candidates == []


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #


async def test_validator_all_pass_returns_reviewed_status():
    llm = _FakeLLM([
        json.dumps({"valid": True, "score": 0.85, "reasoning": "OK", "suggestions": []})
    ] * 4)
    validator = AssessmentValidator(llm_client=llm)
    outcome = await validator.validate(
        question_text="문제", choices=["A", "B"],
        answer="A", explanation="해설",
        difficulty="medium",
    )
    assert outcome.quality_status == "reviewed"
    assert outcome.quality_score == pytest.approx(0.85, abs=1e-3)
    assert len(outcome.validator_results) == 4
    assert all(r["valid"] for r in outcome.validator_results.values())


async def test_validator_invalid_returns_draft_status():
    llm = _FakeLLM([
        json.dumps({"valid": True, "score": 0.85}),
        json.dumps({"valid": False, "score": 0.4, "reasoning": "오류"}),
        json.dumps({"valid": True, "score": 0.8}),
        json.dumps({"valid": True, "score": 0.7}),
    ])
    validator = AssessmentValidator(llm_client=llm)
    outcome = await validator.validate(
        question_text="문제", choices=[], answer="A",
        explanation=None, difficulty="medium",
    )
    assert outcome.quality_status == "draft"


async def test_validator_can_disable_individual_validator():
    llm = _FakeLLM([
        json.dumps({"valid": True, "score": 0.9}),
        json.dumps({"valid": True, "score": 0.8}),
        json.dumps({"valid": True, "score": 0.8}),
    ])  # 3 응답만 — difficulty disabled
    validator = AssessmentValidator(llm_client=llm)
    outcome = await validator.validate(
        question_text="q", choices=[], answer="A",
        explanation=None, difficulty="medium",
        validators_config={"difficulty": {"enable": False}},
    )
    assert outcome.validator_results["difficulty"]["enabled"] is False
    assert outcome.quality_status == "reviewed"
    # 3 응답만 소비 — LLM call 3회
    assert len(llm.calls) == 3


async def test_validator_parses_json_with_trailing_text():
    """7B가 JSON 뒤에 설명을 덧붙이는 케이스(json.loads는 'Extra data'로 실패) —
    견고 파서가 첫 객체만 떼어내 valid 처리한다(멀쩡한 문항이 draft로 안 떨어짐)."""
    resp = '{"valid": true, "score": 0.9, "reasoning": "정답 명확"}\n추가 설명: 이 문제는 적절합니다.'
    llm = _FakeLLM([resp] * 4)
    validator = AssessmentValidator(llm_client=llm)
    outcome = await validator.validate(
        question_text="문제", choices=["A", "B"], answer="A",
        explanation="해설", difficulty="medium",
    )
    assert outcome.quality_status == "reviewed"
    assert all(r["valid"] for r in outcome.validator_results.values())
    assert all(r["parse_error"] is None for r in outcome.validator_results.values())


async def test_validator_retries_on_empty_response():
    """빈 응답은 1회 재시도 — 두 번째에 유효 JSON이 오면 valid로 회복."""
    llm = _FakeLLM(["", json.dumps({"valid": True, "score": 0.8})])
    validator = AssessmentValidator(llm_client=llm)
    cfg = {n: {"enable": False} for n in ("explanation", "choices", "difficulty")}
    outcome = await validator.validate(
        question_text="q", choices=["A"], answer="A",
        explanation=None, difficulty="medium",
        validators_config=cfg,
    )
    assert outcome.validator_results["answer"]["valid"] is True
    assert outcome.validator_results["answer"]["parse_error"] is None
    assert len(llm.calls) == 2  # 1 실패 + 1 재시도


# --------------------------------------------------------------------------- #
# Generate (end-to-end with FakeLLM)
# --------------------------------------------------------------------------- #


async def test_generate_produces_items_with_citations():
    repo = InMemoryAssessmentItemRepository()
    # 참고용 approved item 1건
    await repo.upsert(_item("Q-REF", question_text="기존 문제 본문"))

    items_payload = {
        "items": [
            {
                "question_text": "신규 문제 1",
                "choices": ["A", "B", "C", "D"],
                "answer": "A",
                "explanation": "해설 1",
                "difficulty": "medium",
            },
            {
                "question_text": "신규 문제 2",
                "choices": ["A", "B"],
                "answer": "B",
                "explanation": "해설 2",
                "difficulty": "medium",
            },
        ]
    }
    validator_json = json.dumps(
        {"valid": True, "score": 0.85, "reasoning": "OK", "suggestions": []}
    )
    llm = _FakeLLM([
        json.dumps(items_payload),
        # 2 items × 4 validators = 8 validator calls
        *([validator_json] * 8),
    ])
    embedder = InMemoryEmbedder()
    similarity = AssessmentSimilarityChecker(
        embedder=embedder,
        thresholds=SimilarityThresholds(duplicate=0.99, similar=0.5),
    )
    validator = AssessmentValidator(llm_client=llm)
    service = AssessmentGenerateService(
        repository=repo, llm_client=llm,
        similarity_checker=similarity, validator=validator,
        max_retries=1,
    )
    result = await service.generate(
        domain_id="t1",
        criteria=GenerateCriteria(subject="정보보안", count=2),
    )
    assert result.generated_count == 2
    assert len(result.citations) == 2
    # repository에 저장됨
    items, total = await repo.list_by_tenant(domain_id="t1")
    assert total >= 3  # Q-REF + 2 신규
    new_ones = [r for r in items if r.source == "generated"]
    assert len(new_ones) == 2
    assert all(r.quality_status == "reviewed" for r in new_ones)


async def test_generate_persist_false_does_not_upsert():
    """ADR-027 §6 — 채팅 출제(persist=False)는 DB에 저장하지 않고 result로만 반환."""
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-REF", question_text="기존 문제 본문"))

    items_payload = {
        "items": [
            {
                "question_text": "채팅 출제 문제 1",
                "choices": ["A", "B", "C", "D"],
                "answer": "A",
                "explanation": "해설 1",
                "difficulty": "medium",
            },
        ]
    }
    validator_json = json.dumps(
        {"valid": True, "score": 0.85, "reasoning": "OK", "suggestions": []}
    )
    llm = _FakeLLM([json.dumps(items_payload), *([validator_json] * 4)])
    similarity = AssessmentSimilarityChecker(
        embedder=InMemoryEmbedder(),
        thresholds=SimilarityThresholds(duplicate=0.99, similar=0.5),
    )
    service = AssessmentGenerateService(
        repository=repo, llm_client=llm,
        similarity_checker=similarity, validator=AssessmentValidator(llm_client=llm),
        max_retries=1,
    )
    result = await service.generate(
        domain_id="t1",
        criteria=GenerateCriteria(subject="정보보안", count=1),
        persist=False,
    )
    # 결과로는 문항이 나오되, 채점용 정답·해설을 담고 있다.
    assert result.generated_count == 1
    assert result.items[0].answer == "A"
    assert result.items[0].explanation == "해설 1"
    # DB에는 신규 generated 문항이 저장되지 않는다 (Q-REF만 존재).
    items, total = await repo.list_by_tenant(domain_id="t1")
    assert total == 1
    assert all(r.source != "generated" for r in items)


class _FakeItemIndex:
    def __init__(self, hits):
        self._hits = hits
        self.searched = []

    async def search_similar(self, *, domain_id, question_text, subject=None,
                             top_k=10, exclude_item_id=None):
        self.searched.append(question_text)
        return list(self._hits)


def _gen_service_with_index(repo, llm, index):
    similarity = AssessmentSimilarityChecker(
        embedder=InMemoryEmbedder(),
        thresholds=SimilarityThresholds(duplicate=0.85, similar=0.65),
    )
    return AssessmentGenerateService(
        repository=repo, llm_client=llm,
        similarity_checker=similarity, validator=AssessmentValidator(llm_client=llm),
        max_retries=1, item_index=index,
    )


async def test_generate_uses_index_and_rejects_duplicate():
    """ADR-025 §5 — item_index 주입 시 사전 인덱스 검색으로 dedup. 고유사도(>=dup) → reject."""
    repo = InMemoryAssessmentItemRepository()
    payload = {"items": [{"question_text": "중복 후보 문제", "choices": ["A", "B", "C", "D"],
                          "answer": "A", "difficulty": "medium"}]}
    llm = _FakeLLM([json.dumps(payload), *([json.dumps({"valid": True, "score": 0.9})] * 4)])
    index = _FakeItemIndex([{"item_id": "DUP-1", "subject": "정보보안", "score": 0.92}])
    service = _gen_service_with_index(repo, llm, index)
    result = await service.generate(
        domain_id="t1", criteria=GenerateCriteria(subject="정보보안", count=1),
    )
    assert result.generated_count == 0
    assert result.rejected_duplicates == 1
    assert index.searched  # 사전 인덱스가 실제로 조회됨


async def test_generate_with_index_accepts_when_not_duplicate():
    repo = InMemoryAssessmentItemRepository()
    payload = {"items": [{"question_text": "고유한 신규 문제", "choices": ["A", "B", "C", "D"],
                          "answer": "A", "difficulty": "medium"}]}
    llm = _FakeLLM([json.dumps(payload), *([json.dumps({"valid": True, "score": 0.9})] * 4)])
    # 낮은 유사도(0.4 < similar 0.65) → 중복 아님 + reference 비어있음
    index = _FakeItemIndex([{"item_id": "FAR-1", "subject": "정보보안", "score": 0.4}])
    service = _gen_service_with_index(repo, llm, index)
    result = await service.generate(
        domain_id="t1", criteria=GenerateCriteria(subject="정보보안", count=1),
    )
    assert result.generated_count == 1
    assert result.rejected_duplicates == 0
    items, _ = await repo.list_by_tenant(domain_id="t1")
    new = [r for r in items if r.source == "generated"]
    assert new[0].reference_item_ids == []


async def test_generate_rejects_duplicates_and_retries():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_item("Q-REF", question_text="reference baseline"))

    duplicate_payload = {
        "items": [{
            "question_text": "reference baseline",  # 동일 텍스트 → 중복 reject
            "choices": ["A", "B"], "answer": "A", "explanation": "x",
            "difficulty": "medium",
        }]
    }
    fresh_payload = {
        "items": [{
            "question_text": "fresh original distinct",
            "choices": ["A", "B"], "answer": "A", "explanation": "y",
            "difficulty": "medium",
        }]
    }
    validator_json = json.dumps({"valid": True, "score": 0.9})
    llm = _FakeLLM([
        json.dumps(duplicate_payload),
        json.dumps(fresh_payload),
        *([validator_json] * 4),
    ])

    # 결정론 embedder — 텍스트 ID별로 직교에 가까운 unit vector를 반환
    class _StubEmbedder:
        dense_dim = 4

        @property
        def model_name(self) -> str:
            return "stub"

        async def embed_batch(self, texts):
            mapping = {
                "reference baseline": [1.0, 0.0, 0.0, 0.0],
                "fresh original distinct": [0.0, 1.0, 0.0, 0.0],
            }
            return [(mapping.get(t, [0.0, 0.0, 0.0, 1.0]), {}) for t in texts]

        async def embed_query(self, text):
            res = await self.embed_batch([text])
            return res[0]

    similarity = AssessmentSimilarityChecker(
        embedder=_StubEmbedder(),
        thresholds=SimilarityThresholds(duplicate=0.85, similar=0.5),
    )
    validator = AssessmentValidator(llm_client=llm)
    service = AssessmentGenerateService(
        repository=repo, llm_client=llm,
        similarity_checker=similarity, validator=validator,
        max_retries=2,
    )
    result = await service.generate(
        domain_id="t1",
        criteria=GenerateCriteria(subject="정보보안", count=1),
    )
    assert result.rejected_duplicates == 1
    assert result.generated_count == 1
    assert result.retries_used == 2  # 1차 reject → 2차 성공
