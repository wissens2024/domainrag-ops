"""ExamPaperParser 단위 테스트 (ADR-025 §2) — 순수 텍스트→문항 파싱."""

from __future__ import annotations

from rag_core.services.assessment_exam_parser import ExamPaperParser

# 정답표: [번호 블록][정답 글리프 블록] 반복 (과목 헤더는 무시됨).
_ANSWER_PAGE = "\n".join(
    [
        "【1과목】 소프트웨어 설계",
        "1", "2", "①", "②",
        "【2과목】 소프트웨어 개발",
        "3", "4", "③", "④",
    ]
)

# 문제 페이지: 2과목 × 2문항. Q2는 '그림' 의존, Q4는 ③④가 한 줄, Q1 ④엔 푸터 누출.
_QUESTION_PAGE = "\n".join(
    [
        "지식충전소 에듀온(www.eduon.com)",   # noise — 제거 대상
        "【1과목】 소프트웨어 설계",
        "1. 첫 번째 문제의 질문 내용은 무엇인가?",
        "① 보기 일",
        "② 보기 이",
        "③ 보기 삼",
        "④ 보기 사국가기술자격 필기시험문제2022년도 기사 제2회 필기시험(기사)정보처리기사형 별",
        "2. 다음 그림을 보고 옳은 것을 고르시오?",
        "① 가",
        "② 나",
        "③ 다",
        "④ 라",
        "【2과목】 소프트웨어 개발",
        "3. 세 번째 문제의 내용은 무엇인가?",
        "① A",
        "② B",
        "③ C",
        "④ D",
        "4. 네 번째 문제의 질문은 무엇인가?",
        "① 하나",
        "② 둘",
        "③ 셋 ④ 넷",   # 한 줄에 보기 2개 — 분리되어야 함
    ]
)


def _parse():
    parser = ExamPaperParser()
    return parser.parse(page_texts=[_QUESTION_PAGE, _ANSWER_PAGE])


def test_parses_all_items_and_subjects():
    res = _parse()
    assert res.parsed_count == 4
    assert res.answer_key_count == 4
    items = {it.number: it for it in res.items}
    assert items[1].subject == "software_design"
    assert items[2].subject == "software_design"
    assert items[3].subject == "software_dev"
    assert items[4].subject == "software_dev"


def test_answer_glyph_mapping_circled_digits():
    res = _parse()
    assert res.answer_glyph_map == {"①": 1, "②": 2, "③": 3, "④": 4}
    items = {it.number: it for it in res.items}
    # 정답키: 1→①(idx0), 2→②(idx1), 3→③(idx2), 4→④(idx3)
    assert items[1].answer_index == 0 and items[1].answer_value == "보기 일"
    assert items[2].answer_index == 1 and items[2].answer_value == "나"
    assert items[3].answer_index == 2 and items[3].answer_value == "C"
    assert items[4].answer_index == 3 and items[4].answer_value == "넷"


def test_each_item_has_four_choices():
    res = _parse()
    for it in res.items:
        assert len(it.choices) == 4, (it.number, it.choices)


def test_footer_leak_stripped_from_choice():
    res = _parse()
    q1 = next(it for it in res.items if it.number == 1)
    assert q1.choices[3] == "보기 사"  # 푸터 블록 제거됨


def test_multi_marker_line_split():
    res = _parse()
    q4 = next(it for it in res.items if it.number == 4)
    assert q4.choices == ["하나", "둘", "셋", "넷"]
    assert "choices=3" not in q4.flags


def test_figure_dependent_flagged():
    res = _parse()
    q2 = next(it for it in res.items if it.number == 2)
    assert q2.figure_dependent is True
    assert "figure_dependent" in q2.flags
    q1 = next(it for it in res.items if it.number == 1)
    assert q1.figure_dependent is False


def test_noise_line_removed():
    res = _parse()
    # 'eduon' noise 줄이 문항 텍스트에 섞이지 않음
    for it in res.items:
        assert "eduon" not in it.question_text


def test_empty_pages_returns_empty():
    res = ExamPaperParser().parse(page_texts=[])
    assert res.parsed_count == 0 and res.items == []


def test_custom_subject_map_override():
    parser = ExamPaperParser(subject_map={"1": "X", "2": "Y"})
    res = parser.parse(page_texts=[_QUESTION_PAGE, _ANSWER_PAGE])
    items = {it.number: it for it in res.items}
    assert items[1].subject == "X"
    assert items[3].subject == "Y"
