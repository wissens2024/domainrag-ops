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
    detect_language_drift,
    parse_transposed_answer_grid,
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


def _grid_page(numbers, answers):
    """세로 전치 그리드 한 블록: 번호들 세로 + 정답들 세로."""
    return "\n".join([str(x) for x in numbers] + [str(a) for a in answers])


def test_grid_parser_digit_answers():
    # 1~10 번호 뒤 1~4 맨숫자 정답 (LLM이 가장 자주 실패하는 형태)
    page = _grid_page(range(1, 11), [2, 3, 2, 1, 2, 4, 2, 3, 4, 3])
    grid = parse_transposed_answer_grid(["문항 페이지", page])
    assert grid[1] == 1 and grid[2] == 2 and grid[6] == 3 and grid[10] == 2
    assert len(grid) == 10


def test_grid_parser_korean_answers():
    # 가나다라 정답
    page = _grid_page(range(1, 11), ["나", "다", "가", "라", "라", "나", "라", "가", "라", "나"])
    grid = parse_transposed_answer_grid([page])
    assert grid[1] == 1 and grid[3] == 0 and grid[4] == 3 and grid[10] == 1


def test_grid_parser_multiple_blocks_with_headers():
    # 과목 헤더가 블록을 끊어도 각 블록을 독립 복구
    page = "\n".join([
        "제1과목 : 데이터베이스",
        _grid_page(range(1, 11), [1, 2, 3, 4, 1, 2, 3, 4, 1, 2]),
        "제2과목 : 운영체제",
        _grid_page(range(11, 21), ["①", "②", "③", "④", "①", "②", "③", "④", "①", "②"]),
    ])
    grid = parse_transposed_answer_grid([page])
    assert len(grid) == 20
    assert grid[1] == 0 and grid[11] == 0 and grid[14] == 3


def test_grid_parser_ignores_body_numbering():
    # 본문의 "1." 번호·붙은 보기 마커는 그리드로 오검출하지 않음
    body = "\n".join(["1. 어떤 문제인가?", "① 보기일", "② 보기이", "③ 보기삼", "④ 보기사"])
    assert parse_transposed_answer_grid([body]) == {}


def test_llm_extractor_uses_grid_when_present():
    # LLM 정답표가 비어도 전치 그리드가 있으면 정답 검증됨 (전치형 PDF 시나리오)
    questions = {"items": [
        {"number": n, "question_text": f"문제{n}", "choices": ["A", "B", "C", "D"],
         "subject": "database"} for n in (1, 2, 3)
    ]}
    llm = _FakeLLM(questions, {"answers": []})  # LLM 정답표는 비어 있음
    ext = LlmItemExtractor(llm_client=llm)
    answer_grid_page = _grid_page([1, 2, 3], [2, 4, 1])  # ②④① → idx 1,3,0
    res = asyncio.run(
        ext.extract(page_texts=["문항 페이지", answer_grid_page], answer_page_index=1)
    )
    by = {it.number: it for it in res.items}
    assert by[1].answer_index == 1 and "answer_verified" in by[1].flags
    assert by[2].answer_index == 3 and "answer_verified" in by[2].flags
    assert by[3].answer_index == 0 and "answer_verified" in by[3].flags


def test_detect_language_drift():
    # 한글 원문인데 중국어 문항 → drift
    assert detect_language_drift("数据库管理员应该执行的任务与下列哪个无关", source_is_hangul=True) is True
    # 한글 문항 → drift 아님
    assert detect_language_drift("데이터베이스 관리자의 역할은?", source_is_hangul=True) is False
    # 원문이 한글 위주가 아니면(영어 등) 판정하지 않음
    assert detect_language_drift("数据库管理员", source_is_hangul=False) is False
    # 괄호 속 소수 한자(한글 우세)는 오탐하지 않음
    assert detect_language_drift("트랜잭션의 원자성(原子性)이란?", source_is_hangul=True) is False


def test_extract_flags_chinese_drift():
    """한글 원문인데 LLM이 중국어로 번역한 문항은 language_drift 플래그로 표식
    (auto_approve에서 제외되도록). 근본원인은 프롬프트로 줄이고 잔여는 가드로 차단."""
    questions = {"items": [{
        "number": 1,
        "question_text": "数据库管理员(DBA)应该执行的任务与下列哪个无关？",
        "choices": ["A", "B", "C", "D"], "subject": "database",
    }]}
    llm = _FakeLLM(questions, {"answers": [{"number": 1, "answer": "①"}]})
    ext = LlmItemExtractor(llm_client=llm)
    korean_src = "1. 데이터베이스 관리자가 수행해야 하는 역할로 거리가 먼 것은? 가 나 다 라\n" * 4
    res = asyncio.run(ext.extract(page_texts=[korean_src, "정답표"], answer_page_index=1))
    by = {it.number: it for it in res.items}
    assert "language_drift" in by[1].flags


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
