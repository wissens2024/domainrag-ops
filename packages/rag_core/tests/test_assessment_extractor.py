"""LlmItemExtractor / RuleBasedExamExtractor 단위 테스트 (ADR-026).

fake LLMClient로 추출 오케스트레이션 + 정답표 교차검증 + 라벨 매핑을 검증.
실제 모델 품질은 운영 vLLM 통합 검증으로 다룬다.
"""

from __future__ import annotations

import asyncio
import json

from rag_core.services.assessment_extractor import (
    LlmItemExtractor,
    RuleBasedExamExtractor,
    answer_label_to_index,
)


class _FakeLLM:
    def __init__(self, questions: dict, answers: dict) -> None:
        self._q = json.dumps(questions, ensure_ascii=False)
        self._a = json.dumps(answers, ensure_ascii=False)
        self.calls = 0

    async def generate(self, prompt, *, model, max_tokens=1024, temperature=0.2,
                       guided_json_schema=None, lora_adapter=None) -> str:  # noqa: ANN001
        self.calls += 1
        if "[정답표 텍스트]" in prompt:
            return self._a
        return self._q

    async def stream(self, *a, **k):  # pragma: no cover
        yield ""

    async def health(self) -> bool:
        return True


_QUESTIONS = {
    "items": [
        {"number": 1, "question_text": "DBMS란?", "choices": ["A", "B", "C", "D"],
         "subject": "database"},
        {"number": 2, "question_text": "구 과목 문제?", "choices": ["가", "나", "다", "라"],
         "subject": "legacy:전자계산기구조"},
    ]
}
_ANSWERS = {"answers": [{"number": 1, "answer": "②"}, {"number": 2, "answer": "4"}]}


def _extract(questions=_QUESTIONS, answers=_ANSWERS, pages=None):
    llm = _FakeLLM(questions, answers)
    ext = LlmItemExtractor(llm_client=llm)
    pages = pages or ["문항 페이지 텍스트", "정답표 페이지 텍스트"]
    return asyncio.run(ext.extract(page_texts=pages, answer_page_index=1)), llm


def test_label_mapping():
    assert answer_label_to_index("①") == 0
    assert answer_label_to_index("2") == 1
    assert answer_label_to_index("다") == 2
    assert answer_label_to_index("d") == 3
    assert answer_label_to_index(None) is None
    assert answer_label_to_index("⑤") is None


def test_llm_extract_items_and_verify_answers():
    res, _ = _extract()
    assert res.parsed_count == 2
    assert res.answer_key_count == 2
    by = {it.number: it for it in res.items}
    # 1번: subject 매핑, 정답 ②(idx1)=B, 검증됨
    assert by[1].subject == "database"
    assert by[1].answer_index == 1 and by[1].answer_value == "B"
    assert "answer_verified" in by[1].flags
    # 2번: legacy 과목 → enum 미매핑(None)이나 label 보존, 정답 4(idx3)=라
    assert by[2].subject is None
    assert by[2].subject_label == "legacy:전자계산기구조"
    assert by[2].answer_index == 3 and by[2].answer_value == "라"
    assert "answer_verified" in by[2].flags


def test_llm_extract_unverified_when_no_answer_key():
    res, _ = _extract(answers={"answers": []})
    by = {it.number: it for it in res.items}
    assert by[1].answer_index is None
    assert "answer_unverified" in by[1].flags
    assert res.answer_key_count == 0


def test_llm_extract_handles_codefenced_json():
    # 모델이 코드펜스로 감싼 경우도 파싱
    llm = _FakeLLM(_QUESTIONS, _ANSWERS)
    llm._q = "```json\n" + llm._q + "\n```"
    ext = LlmItemExtractor(llm_client=llm)
    res = asyncio.run(ext.extract(page_texts=["q", "a"], answer_page_index=1))
    assert res.parsed_count == 2


def test_question_aware_chunking_keeps_questions_whole():
    import re as _re
    ext = LlmItemExtractor(llm_client=_FakeLLM({}, {}), max_chars_per_call=30)
    text = "\n".join([
        "1. 첫 번째 문제 지문 길게", "① a", "② b",
        "2. 두 번째 문제 지문 길게", "① c", "② d",
        "3. 세 번째 문제 지문", "① e",
    ])
    chunks = ext._chunks(text)
    assert len(chunks) >= 2
    # 모든 청크는 문항 번호 줄로 시작 — 문항 중간 절단 없음
    for ch in chunks:
        assert _re.match(r"^\s*\d+\.\s", ch.split("\n")[0])
    # 2번 문항이 한 청크 안에 온전히 (지문+보기)
    assert any("2. 두 번째 문제 지문 길게" in c and "② d" in c for c in chunks)


def test_rule_based_extractor_delegates():
    qpage = "\n".join([
        "【1과목】 소프트웨어 설계",
        "1. 첫 문제의 질문 내용은 무엇인가?",
        "① 일", "② 이", "③ 삼", "④ 사",
    ])
    apage = "\n".join(["1", "①"])
    ext = RuleBasedExamExtractor()
    res = asyncio.run(ext.extract(page_texts=[qpage, apage], answer_page_index=1))
    assert res.parsed_count == 1
    assert res.items[0].subject == "software_design"
    assert res.items[0].answer_index == 0
