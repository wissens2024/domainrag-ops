"""PyMuPdfExamExtractor 컬럼 인식 링크 단위 테스트 (ADR-025 §2).

fitz가 필요한 extract()는 실 PDF 통합 검증으로 다루고, 여기서는 2단 레이아웃에서
그림→문항 귀속의 순수 로직(_nearest_question)을 고정한다.
"""

from __future__ import annotations

from app.services.assessment_pdf_extractor import PyMuPdfExamExtractor

# 실 PDF(2022-2회) page4 좌표 재현: 좌측단 x≈58, 우측단 x≈322. mid_x≈297.
_QPOS = [
    (30, 58.0, 62.0), (35, 322.0, 104.0), (31, 58.0, 216.0), (36, 322.0, 271.0),
    (32, 58.0, 357.0), (37, 322.0, 425.0), (33, 58.0, 458.0), (38, 322.0, 608.0),
    (39, 322.0, 692.0), (34, 58.0, 705.0),
]
_MID = 297.0


def test_right_column_figure_links_to_right_question():
    # Q37 트리(우측단 x=322, y=458) — 같은 y의 좌측단 Q33이 아니라 Q37에 귀속
    n = PyMuPdfExamExtractor._nearest_question(
        _QPOS, fig_x0=322.0, fig_y0=458.0, mid_x=_MID
    )
    assert n == 37


def test_left_column_figure_links_to_left_question():
    n = PyMuPdfExamExtractor._nearest_question(
        _QPOS, fig_x0=58.0, fig_y0=509.0, mid_x=_MID
    )
    assert n == 33


def test_fallback_when_no_same_column_above():
    # 좌측단 최상단보다 위 → 같은 컬럼 후보 없음 → fallback(첫 문항)
    n = PyMuPdfExamExtractor._nearest_question(
        _QPOS, fig_x0=58.0, fig_y0=40.0, mid_x=_MID
    )
    assert n == 30


def test_empty_positions_returns_none():
    assert PyMuPdfExamExtractor._nearest_question(
        [], fig_x0=100.0, fig_y0=100.0, mid_x=_MID
    ) is None
