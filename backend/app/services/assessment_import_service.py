"""AssessmentImportService — 기출 PDF를 draft item으로 적재 (ADR-025 §2).

오케스트레이션:
  extractor(fitz) → page_texts + figures
  parser(rag_core) → ParsedExamItem
  figures를 near_question_number로 그룹핑 → 문항별 자산
  각 figure PNG를 DocumentStorage(MinIO, ADR-024 암호화)에 저장 → asset 메타
  figure_dependent 확정(그림 링크 OR 텍스트 힌트) → AssessmentItemRecord upsert(source='imported')

dedup(ADR-025 §5)·자동 승인 게이팅은 후속 Phase. 본 서비스는 기본 draft로 적재하며,
모든 결과는 사람 검수(Review Queue) 전제다.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.assessment_item_repository import AssessmentItemRecord
from rag_core.services.assessment_extractor import (
    AssessmentItemExtractor,
    RuleBasedExamExtractor,
)

from app.services.assessment_pdf_extractor import ExtractedPdf, PyMuPdfExamExtractor
from app.services.document_storage import DocumentStorage


@dataclass
class ImportItemResult:
    item_id: str
    number: int
    subject: str | None
    figure_dependent: bool
    asset_count: int
    quality_status: str
    has_answer: bool
    answer_verified: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    created: int = 0
    figures_stored: int = 0
    parsed_count: int = 0
    answer_key_count: int = 0
    items: list[ImportItemResult] = field(default_factory=list)


class AssessmentImportService:
    def __init__(
        self,
        *,
        repository,
        storage: DocumentStorage,
        extractor: Any | None = None,
        item_extractor: AssessmentItemExtractor | None = None,
    ) -> None:
        self._repo = repository
        self._storage = storage
        self._extractor = extractor or PyMuPdfExamExtractor()
        # ADR-026 — 텍스트→문항 추출 어댑터(규칙/LLM). 기본은 규칙(고정포맷). deps가
        # configs에 따라 LlmItemExtractor를 주입해 포맷 무관 추출로 전환한다.
        self._item_extractor = item_extractor or RuleBasedExamExtractor()

    async def import_pdf(
        self,
        *,
        domain_id: str,
        pdf_bytes: bytes,
        item_id_prefix: str,
        answer_page_index: int | None = None,
        default_quality_status: str = "draft",
        tags: list[str] | None = None,
        auto_approve: bool = False,
    ) -> ImportResult:
        """auto_approve=True면 정답표 교차검증 통과(answer_verified) + 보기 4개 정상
        문항만 approved, 나머지는 default_quality_status(draft)로 분리한다 (ADR-026 §4)."""
        extracted: ExtractedPdf = self._extractor.extract(pdf_bytes)
        n_pages = len(extracted.page_texts)
        ans_idx = answer_page_index if answer_page_index is not None else n_pages - 1

        parsed = await self._item_extractor.extract(
            page_texts=extracted.page_texts, answer_page_index=ans_idx
        )

        # 정답 페이지의 그림(격자 오탐 등)은 제외하고 문항별로 그룹핑
        figs_by_q: dict[int, list] = {}
        for fig in extracted.figures:
            if (fig.page_number - 1) == ans_idx:
                continue
            if fig.near_question_number is None:
                continue
            figs_by_q.setdefault(fig.near_question_number, []).append(fig)

        result = ImportResult(
            parsed_count=parsed.parsed_count,
            answer_key_count=parsed.answer_key_count,
        )

        for item in parsed.items:
            item_id = f"{item_id_prefix}{item.number:03d}"
            figs = figs_by_q.get(item.number, [])
            assets = []
            for fig in figs:
                asset = await self._store_figure(
                    domain_id=domain_id, item_id=item_id, fig=fig
                )
                assets.append(asset)
                result.figures_stored += 1

            figure_dependent = bool(figs) or item.figure_dependent
            # 정답 확정(정답표 매핑/교차검증) + 보기 4개 정상 = 검증됨. 규칙·LLM 어댑터 공통.
            answer_verified = item.answer_index is not None and len(item.choices) == 4
            # ADR-026 §4 — 검증된 문항만 자동 승인, 나머지 draft
            qstatus = default_quality_status
            if auto_approve and answer_verified:
                qstatus = "approved"
            record = AssessmentItemRecord(
                item_id=item_id,
                domain_id=domain_id,
                subject=item.subject,
                question_type="multiple_choice",
                question_text=item.question_text,
                choices=item.choices,
                answer=item.answer_value or "",
                tags=list(tags or []),
                assets=assets,
                figure_dependent=figure_dependent,
                quality_status=qstatus,
                source="imported",
            )
            await self._repo.upsert(record)
            result.created += 1
            result.items.append(
                ImportItemResult(
                    item_id=item_id,
                    number=item.number,
                    subject=item.subject,
                    figure_dependent=figure_dependent,
                    asset_count=len(assets),
                    quality_status=qstatus,
                    has_answer=bool(item.answer_value),
                    answer_verified=answer_verified,
                    flags=item.flags,
                )
            )
        return result

    async def _store_figure(
        self, *, domain_id: str, item_id: str, fig
    ) -> dict[str, Any]:
        asset_id = uuid.uuid4().hex[:12]
        png = fig.png_bytes
        stored = await self._storage.save(
            domain_id=domain_id,
            doc_id=item_id,
            version="assets",
            filename=f"{asset_id}.png",
            stream=io.BytesIO(png),
        )
        return {
            "asset_id": asset_id,
            "kind": "image",
            "storage_key": stored.object_storage_path,
            "content_hash": "sha256:" + hashlib.sha256(png).hexdigest(),
            "source_page": fig.page_number,
            "bbox": list(fig.bbox),
            "vlm_description": None,
        }
