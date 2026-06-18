"""AssessmentImportService 오케스트레이션 테스트 (ADR-025 §2c).

fitz/PDF 없이 — fake extractor가 canned ExtractedPdf를 반환하고, InMemory repo +
LocalFilesystemStorage로 파싱→그림 저장→draft item 생성 흐름을 검증한다.
"""

from __future__ import annotations

import asyncio

import pytest

from rag_core.interfaces.assessment_item_repository import (
    InMemoryAssessmentItemRepository,
)
from app.services.assessment_import_service import AssessmentImportService
from app.services.assessment_pdf_extractor import ExtractedFigure, ExtractedPdf
from app.services.document_storage import LocalFilesystemStorage

_QUESTION_PAGE = "\n".join(
    [
        "【1과목】 소프트웨어 설계",
        "1. 첫 번째 문제의 질문 내용은 무엇인가?",
        "① 보기 일",
        "② 보기 이",
        "③ 보기 삼",
        "④ 보기 사",
        "2. 두 번째 문제의 질문 내용은 무엇인가?",
        "① 가",
        "② 나",
        "③ 다",
        "④ 라",
    ]
)
_ANSWER_PAGE = "\n".join(["1", "2", "①", "②"])

_PNG = b"\x89PNG\r\n\x1a\n_fake_tree_image_"


class _FakeExtractor:
    def __init__(self, extracted: ExtractedPdf) -> None:
        self._e = extracted

    def extract(self, pdf_bytes: bytes, **kw) -> ExtractedPdf:  # noqa: ANN003
        return self._e


def _service(tmp_path):
    extracted = ExtractedPdf(
        page_texts=[_QUESTION_PAGE, _ANSWER_PAGE],
        figures=[
            ExtractedFigure(
                page_number=1, bbox=(10.0, 20.0, 80.0, 90.0),
                png_bytes=_PNG, near_question_number=2,
            )
        ],
    )
    repo = InMemoryAssessmentItemRepository()
    storage = LocalFilesystemStorage(base_dir=tmp_path / "store")
    svc = AssessmentImportService(
        repository=repo, storage=storage, extractor=_FakeExtractor(extracted),
    )
    return svc, repo


def test_import_creates_items_and_links_figure(tmp_path):
    svc, repo = _service(tmp_path)
    res = asyncio.run(
        svc.import_pdf(
            domain_id="exam-engineer", pdf_bytes=b"x",
            item_id_prefix="gisa-2022-2-w-", answer_page_index=1,
        )
    )
    assert res.created == 2
    assert res.parsed_count == 2 and res.answer_key_count == 2
    assert res.figures_stored == 1

    by_num = {it.number: it for it in res.items}
    # Q2: 그림 링크 → figure_dependent True, asset 1
    assert by_num[2].figure_dependent is True
    assert by_num[2].asset_count == 1
    assert by_num[2].item_id == "gisa-2022-2-w-002"
    # Q1: 그림 없음
    assert by_num[1].figure_dependent is False
    assert by_num[1].asset_count == 0


def test_imported_item_persisted_with_answer_and_asset(tmp_path):
    svc, repo = _service(tmp_path)
    asyncio.run(
        svc.import_pdf(
            domain_id="exam-engineer", pdf_bytes=b"x",
            item_id_prefix="gisa-2022-2-w-", answer_page_index=1,
        )
    )
    q1 = asyncio.run(repo.get(domain_id="exam-engineer", item_id="gisa-2022-2-w-001"))
    assert q1 is not None
    assert q1.source == "imported"
    assert q1.quality_status == "draft"
    assert q1.answer == "보기 일"  # 정답키 ① → 첫 보기
    assert len(q1.choices) == 4

    q2 = asyncio.run(repo.get(domain_id="exam-engineer", item_id="gisa-2022-2-w-002"))
    assert q2.answer == "나"  # ②
    assert len(q2.assets) == 1
    a = q2.assets[0]
    assert a["kind"] == "image"
    assert a["storage_key"]
    assert a["content_hash"].startswith("sha256:")
    assert a["source_page"] == 1
    assert a["bbox"] == [10.0, 20.0, 80.0, 90.0]


def test_answer_page_figures_excluded(tmp_path):
    """정답 페이지(인덱스1)의 그림은 자산으로 저장하지 않는다."""
    extracted = ExtractedPdf(
        page_texts=[_QUESTION_PAGE, _ANSWER_PAGE],
        figures=[
            ExtractedFigure(
                page_number=2, bbox=(0.0, 0.0, 50.0, 50.0),
                png_bytes=_PNG, near_question_number=1,
            )
        ],
    )
    repo = InMemoryAssessmentItemRepository()
    storage = LocalFilesystemStorage(base_dir=tmp_path / "store")
    svc = AssessmentImportService(
        repository=repo, storage=storage, extractor=_FakeExtractor(extracted),
    )
    res = asyncio.run(
        svc.import_pdf(
            domain_id="exam-engineer", pdf_bytes=b"x",
            item_id_prefix="p-", answer_page_index=1,
        )
    )
    assert res.figures_stored == 0
