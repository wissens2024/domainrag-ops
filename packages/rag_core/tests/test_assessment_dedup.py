"""assessment dedup_key -- 동일 문항 정규화 key (ADR-025 §5)."""

from __future__ import annotations

from rag_core.services.assessment_dedup import dedup_key


def test_identical_question_same_key():
    a = dedup_key("DBMS의 정의는?", ["A", "B", "C", "D"])
    b = dedup_key("DBMS의 정의는?", ["A", "B", "C", "D"])
    assert a == b


def test_whitespace_and_punctuation_ignored():
    a = dedup_key("DBMS의  정의는?", ["가", "나"])
    b = dedup_key("DBMS의 정의는", ["가", "나"])
    assert a == b


def test_choice_order_ignored():
    a = dedup_key("질문", ["A", "B", "C", "D"])
    b = dedup_key("질문", ["D", "C", "B", "A"])
    assert a == b


def test_different_question_different_key():
    a = dedup_key("질문 하나", ["A", "B"])
    b = dedup_key("질문 둘", ["A", "B"])
    assert a != b


def test_different_choices_different_key():
    a = dedup_key("질문", ["A", "B", "C", "D"])
    b = dedup_key("질문", ["A", "B", "C", "E"])
    assert a != b


def test_empty_choices_tolerated():
    # 빈/공백 보기는 무시하고 질문만으로 key 생성 (예외 없이)
    k = dedup_key("질문만 있음", ["", "  "])
    assert isinstance(k, str) and "질문만있음" in k
