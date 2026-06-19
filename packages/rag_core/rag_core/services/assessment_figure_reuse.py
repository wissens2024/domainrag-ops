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
    vlm_errors: int = 0  # ref별 VLM 일시 오류(timeout/5xx) 수 — 다음 ref로 진행(degrade)
    vlm_unavailable: bool = False


def _norm_predicted_difficulty(value: Any) -> str | None:
    """ADR-029 — VLM 출력 예상 난이도를 상/중/하로 정규화."""
    if not value:
        return None
    s = str(value).strip().lower()
    if s in ("상", "high", "hard", "어려움", "어렵"):
        return "상"
    if s in ("하", "low", "easy", "쉬움", "쉬"):
        return "하"
    if s in ("중", "medium", "mid", "보통"):
        return "중"
    return None


def _build_vlm_prompt(ref) -> str:
    """그림 + **원본 문제 맥락**을 함께 주어 VLM이 그림이 평가하는 개념을 알고 새 문제를
    만들게 한다(ADR-027). 그림만 주면 워터마크·표면(숫자/글자)을 묻는 무의미 문항이 나온다."""
    choices = " / ".join(str(c) for c in (getattr(ref, "choices", None) or []))
    lines = [
        "아래 그림은 정보처리기사 시험 문제에 사용된 그림이다.",
        "이 그림이 쓰인 원본 문제(참고용 — 그대로 베끼지 말 것):",
        f"- 원본 문제: {getattr(ref, 'question_text', '')}",
    ]
    if choices:
        lines.append(f"- 원본 보기: {choices}")
    if getattr(ref, "answer", ""):
        lines.append(f"- 원본 정답: {ref.answer}")
    if getattr(ref, "explanation", None):
        lines.append(f"- 원본 해설: {ref.explanation}")
    lines += [
        "",
        "위 원본 문제와 **같은 그림·같은 개념**을 평가하는 새로운 4지선다 객관식 문제 1개를 한국어로 만들어라.",
        "- 원본이 묻는 핵심 개념(예: 트리 순회 결과, 회로 동작, 자료구조 특성)을 유지하되, "
        "질문 표현·보기·정답은 새로 구성한다.",
        "- 그림에 찍힌 워터마크·로고·출처 표시(Gisapass, 기사패스, 사이트 주소 등)는 절대 문제의 "
        "소재로 쓰지 마라. 그것을 세거나 묻는 문제는 만들지 마라.",
        "- 보기 4개는 서로 모두 달라야 한다(동일 보기 금지).",
        "- 출력은 반드시 한국어로 하라. 다른 언어로 번역·치환하지 마라.",
        '- predicted_difficulty: 이 문항의 체감 난이도를 "상"/"중"/"하"로 독립 평가하라(상=어려움).',
        '- JSON만 출력: {"question_text": str, "choices": [보기 4개 str], '
        '"answer": 정답 보기 텍스트(choices 중 하나와 정확히 일치), "explanation": str, '
        '"predicted_difficulty": "상|중|하"}',
    ]
    return "\n".join(lines)


# 시험지 crop에 섞이는 워터마크/출처 브랜드 — 이를 묻는 문항은 무의미하므로 폐기(ADR-025 #3).
_WATERMARK_HINTS = ("gisapass", "기사패스", "gisafirst", "comcbt")


def _references_watermark(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _WATERMARK_HINTS)


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
        self, *, domain_id: str, criteria: FigureReuseCriteria, persist: bool = True
    ) -> FigureReuseResult:
        """ADR-025 §3b·§4 — 그림 재사용 생성.

        persist=False(ADR-027): 채팅 그림 출제용 — 생성 문항을 DB에 저장하지 않고
        result로만 반환(ephemeral). 콘솔은 persist=True(기본)로 문제은행에 draft 적재.
        """
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

            try:
                raw = await self._vlm.describe(
                    image=image, prompt=_build_vlm_prompt(ref), model=self._model,
                    max_tokens=700, temperature=0.4,
                )
            except Exception:  # noqa: BLE001 — VLM 일시 오류(timeout/5xx)는 이 ref만
                # 건너뛰고 다음 ref를 시도한다(ADR-025 §4 degrade). 전체 generate를
                # abort시키면 호출측이 '그림 없음'으로 오인한다(근본 원인).
                result.vlm_errors += 1
                continue
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
            # 중복 보기 폐기(예: 4개가 모두 'Gisapass'). 정규화 후 distinct 4개 필요.
            if len({c.strip().lower() for c in choices}) < 4:
                result.rejected_invalid += 1
                continue
            # 워터마크/출처 브랜드(시험지 로고)를 묻는 문항 폐기 — crop에 섞인 워터마크를
            # VLM이 콘텐츠로 오인한 것이라 시험 문항으로 무의미하다(ADR-025 #3 후속).
            _blob = qtext + " " + " ".join(choices) + " " + str(parsed.get("explanation") or "")
            if _references_watermark(_blob):
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
                predicted_difficulty=_norm_predicted_difficulty(
                    parsed.get("predicted_difficulty")
                ),
            )
            # ADR-027 — 채팅 출제(persist=False)는 DB 저장 생략(ephemeral, 문제은행 오염 방지).
            if persist:
                await self._repo.upsert(item)
            produced.append(item)
            result.references_used.append(ref.item_id)

        result.items = produced
        result.generated_count = len(produced)
        return result
