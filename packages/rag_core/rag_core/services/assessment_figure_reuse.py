"""AssessmentFigureReuseGenerator — 기존 그림 재사용 + 새 질문 생성 (ADR-025 §3b·§4).

approved이고 figure_dependent인 참조 문항의 *그림을 고정*한 채, VLM(Qwen-VL)이 그림을
이해해 같은 그림에 대한 새 4지선다 Q&A를 만든다. 신규 그림 합성은 하지 않는다 —
asset은 참조 문항 것을 그대로 승계한다(ADR-025 §3: 자유형 새 그림 비지원).

생성물은 항상 draft(Y2: generated는 자동 승인 없음). VLM 비가동 시 빈 결과로 degrade한다.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
    AssessmentItemRepository,
    ExtractCriteria,
)
from rag_core.interfaces.vision_language_client import VisionLanguageClient

# storage_key → 이미지 bytes (backend가 DocumentStorage.load로 배선; rag_core→backend 의존 회피).
ImageLoader = Callable[[str], Awaitable[bytes]]


@dataclass
class FigureReuseCriteria:
    subject: str
    chapter: str | None = None
    difficulty: str = "medium"
    count: int = 3


@dataclass
class FigureReuseResult:
    items: list[AssessmentItemRecord] = field(default_factory=list)
    generated_count: int = 0
    references_used: list[str] = field(default_factory=list)
    skipped_no_image: int = 0
    rejected_invalid: int = 0
    vlm_unavailable: bool = False


_PROMPT = (
    "이 그림을 보고, 같은 그림에 대한 새로운 4지선다 객관식 문제 1개를 한국어로 만들어라.\n"
    "- 그림에서 실제로 확인 가능한 내용만 묻는다. 그림에 없는 것을 지어내지 마라.\n"
    "- 출력은 반드시 한국어로 하라. 다른 언어로 번역·치환하지 마라.\n"
    '- JSON만 출력: {"question_text": str, "choices": [보기 4개 str], '
    '"answer": 정답 보기 텍스트(choices 중 하나와 정확히 일치), "explanation": str}\n'
)


def _parse_json_obj(raw: str) -> dict | None:
    """코드펜스·trailing 텍스트를 허용하는 견고 파서 (assessment_validator와 동일 전략)."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    i = s.find("{")
    if i >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[i:])
            return obj if isinstance(obj, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


class AssessmentFigureReuseGenerator:
    def __init__(
        self,
        *,
        repository: AssessmentItemRepository,
        vlm: VisionLanguageClient,
        image_loader: ImageLoader,
        model: str | None = None,
    ) -> None:
        self._repo = repository
        self._vlm = vlm
        self._load_image = image_loader
        self._model = model

    async def generate(
        self, *, domain_id: str, criteria: FigureReuseCriteria
    ) -> FigureReuseResult:
        result = FigureReuseResult()
        # ADR-025 §4 degrade — VLM 비가동이면 그림 기반 생성 skip.
        if not await self._vlm.health():
            result.vlm_unavailable = True
            return result

        candidates = await self._repo.list_candidates_for_extract(
            domain_id=domain_id,
            criteria=ExtractCriteria(
                subject=criteria.subject,
                chapter=criteria.chapter,
                quality_status=["approved"],
            ),
            limit=200,
        )
        refs = [c for c in candidates if c.figure_dependent and c.assets]

        produced: list[AssessmentItemRecord] = []
        for ref in refs:
            if len(produced) >= criteria.count:
                break
            key = (ref.assets[0] or {}).get("storage_key")
            if not key:
                result.skipped_no_image += 1
                continue
            try:
                image = await self._load_image(key)
            except Exception:  # noqa: BLE001 — 자산 누락/읽기 실패는 skip
                result.skipped_no_image += 1
                continue

            raw = await self._vlm.describe(
                image=image, prompt=_PROMPT, model=self._model,
                max_tokens=700, temperature=0.4,
            )
            parsed = _parse_json_obj(raw)
            if not parsed:
                result.rejected_invalid += 1
                continue
            qtext = str(parsed.get("question_text", "")).strip()
            choices = [str(c).strip() for c in (parsed.get("choices") or []) if str(c).strip()]
            answer = str(parsed.get("answer", "")).strip()
            # 그림 의존 문항도 4지선다 + 정답이 보기 중 하나여야 신뢰 가능.
            if not qtext or len(choices) != 4 or answer not in choices:
                result.rejected_invalid += 1
                continue

            item = AssessmentItemRecord(
                item_id=f"Q-{uuid.uuid4().hex[:12]}",
                domain_id=domain_id,
                subject=ref.subject or criteria.subject,
                chapter=ref.chapter or criteria.chapter,
                difficulty=criteria.difficulty,
                question_type="multiple_choice",
                question_text=qtext,
                choices=choices,
                answer=answer,
                explanation=str(parsed.get("explanation", "")) or None,
                tags=["figure_reuse"],
                quality_status="draft",  # Y2 — generated는 자동 승인 없음
                source="generated",
                reference_item_ids=[ref.item_id],
                assets=ref.assets,          # 같은 그림 승계 (신규 합성 아님)
                figure_dependent=True,
            )
            await self._repo.upsert(item)
            produced.append(item)
            result.references_used.append(ref.item_id)

        result.items = produced
        result.generated_count = len(produced)
        return result
