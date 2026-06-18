"""AssessmentItemExtractor — 기출 PDF에서 문항을 추출하는 어댑터 계층 (ADR-026).

규칙 기반(`RuleBasedExamExtractor`, 고정 포맷 고속)과 LLM 기반(`LlmItemExtractor`,
포맷 무관 범용)을 같은 Protocol로 둔다. import 파이프라인(ADR-025 §2)은 어댑터를
주입받아 동작하므로, 포맷이 달라져도 코드 수정 없이 어댑터만 바꾸면 된다.

LLM 추출(ADR-026):
  - 문항 페이지 텍스트를 청크로 나눠 guided JSON 스키마로 문항 추출.
  - 정답표 페이지는 별도 LLM 호출로 {번호→정답라벨} 추출.
  - 정답라벨(①②③④ / 1~4 / 가나다라 / a~d)을 보기 인덱스로 매핑 → answer 확정.
  - 정답표가 매핑되면 answer_verified=True, 아니면 False(검수 대상, ADR-026 §3).
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from rag_core.interfaces.llm_client import LLMClient
from rag_core.services.assessment_exam_parser import (
    DEFAULT_GISA_SUBJECT_MAP,
    ExamParseResult,
    ExamPaperParser,
    ParsedExamItem,
)

# 정답 라벨 → 0-based 보기 인덱스. 출처마다 ①②③④ / 1~4 / 가나다라 / a~d 혼재.
_LABEL_TO_IDX: dict[str, int] = {}
for _i, _grp in enumerate(
    [("①", "1", "가", "a", "A"), ("②", "2", "나", "b", "B"),
     ("③", "3", "다", "c", "C"), ("④", "4", "라", "d", "D")]
):
    for _t in _grp:
        _LABEL_TO_IDX[_t] = _i

_SUBJECT_ENUMS = set(DEFAULT_GISA_SUBJECT_MAP.values())


def answer_label_to_index(label: str | None) -> int | None:
    """정답 라벨을 0-based 인덱스로. 매핑 불가 시 None."""
    if not label:
        return None
    s = str(label).strip()
    if s in _LABEL_TO_IDX:
        return _LABEL_TO_IDX[s]
    # 라벨이 보기 텍스트 전체일 수도 있음 → caller가 텍스트 매칭으로 보강
    return None


class AssessmentItemExtractor(Protocol):
    async def extract(
        self, *, page_texts: list[str], answer_page_index: int | None = None
    ) -> ExamParseResult: ...


class RuleBasedExamExtractor:
    """고정 포맷 고속 어댑터 — 기존 ExamPaperParser를 Protocol에 맞춤(동기→비동기 래핑)."""

    def __init__(self, parser: ExamPaperParser | None = None) -> None:
        self._parser = parser or ExamPaperParser()

    async def extract(
        self, *, page_texts: list[str], answer_page_index: int | None = None
    ) -> ExamParseResult:
        return self._parser.parse(
            page_texts=page_texts, answer_page_index=answer_page_index
        )


_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "question_text": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                },
                "required": ["number", "question_text", "choices"],
            },
        }
    },
    "required": ["items"],
}

_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "answer": {"type": "string"},
                },
                "required": ["number", "answer"],
            },
        }
    },
    "required": ["answers"],
}

_QUESTION_PROMPT = (
    "다음은 시험 문제지의 일부다. 모든 문항을 추출해 JSON으로 반환하라.\n"
    "- number: 문항 번호(정수)\n"
    "- question_text: 지문 전체(보기 제외)\n"
    "- choices: 보기 텍스트 배열(마커 ①②③④ 제외, 순서 유지)\n"
    "- subject: 다음 중 하나로 매핑 가능하면 그 값, 아니면 'legacy:<원문 과목명>'\n"
    f"  {sorted(_SUBJECT_ENUMS)}\n"
    "지문/보기의 줄바꿈 공백을 자연스럽게 복원하라. 없는 내용을 지어내지 마라.\n\n"
    "[문제지 텍스트]\n{chunk}\n"
)

_ANSWER_PROMPT = (
    "다음은 시험 정답표다. 각 문항 번호의 정답을 JSON으로 반환하라.\n"
    "- number: 문항 번호(정수)\n"
    "- answer: 정답 라벨(①②③④ 또는 1~4). 표에 있는 그대로.\n"
    "없는 번호를 지어내지 마라.\n\n"
    "[정답표 텍스트]\n{text}\n"
)


def _parse_json(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # 코드펜스/잡텍스트 둘러싸인 경우 JSON 본문만 추출 시도
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


class LlmItemExtractor:
    """포맷 무관 LLM 추출기 (ADR-026 §2·§3). 정확도는 정답표 교차검증으로 보강."""

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        model: str = "shared_llm",
        max_chars_per_call: int = 2000,  # vLLM max_model_len(예 4096) 대비 안전 청크
        max_tokens: int = 1500,
    ) -> None:
        self._llm = llm_client
        self._model = model
        self._max_chars = max_chars_per_call
        self._max_tokens = max_tokens

    async def extract(
        self, *, page_texts: list[str], answer_page_index: int | None = None
    ) -> ExamParseResult:
        if not page_texts:
            return ExamParseResult()
        ans_idx = (
            answer_page_index if answer_page_index is not None else len(page_texts) - 1
        )
        question_pages = [t for i, t in enumerate(page_texts) if i != ans_idx]
        answer_page = page_texts[ans_idx] if 0 <= ans_idx < len(page_texts) else ""

        # 1. 문항 추출 (청크 단위)
        items_by_num: dict[int, ParsedExamItem] = {}
        for chunk in self._chunks("\n".join(question_pages)):
            raw = await self._llm.generate(
                _QUESTION_PROMPT.format(chunk=chunk),
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0.0,
                guided_json_schema=_QUESTION_SCHEMA,
            )
            parsed = _parse_json(raw) or {}
            for it in parsed.get("items", []):
                num = it.get("number")
                if not isinstance(num, int):
                    continue
                choices = [str(c).strip() for c in (it.get("choices") or []) if str(c).strip()]
                subj = it.get("subject")
                enum = subj if subj in _SUBJECT_ENUMS else None
                items_by_num[num] = ParsedExamItem(
                    number=num,
                    subject=enum,
                    subject_label=subj,
                    question_text=str(it.get("question_text", "")).strip(),
                    choices=choices,
                    answer_index=None,
                    answer_value=None,
                    flags=[] if len(choices) == 4 else [f"choices={len(choices)}"],
                )

        # 2. 정답표 추출
        answer_idx_by_num = await self._extract_answer_key(answer_page)

        # 3. 정답 매핑 + 교차검증 (ADR-026 §3)
        for num, item in items_by_num.items():
            ai = answer_idx_by_num.get(num)
            if ai is not None and len(item.choices) == 4 and 0 <= ai < 4:
                item.answer_index = ai
                item.answer_value = item.choices[ai]
                item.flags.append("answer_verified")
            else:
                item.flags.append("answer_unverified")

        items = [items_by_num[n] for n in sorted(items_by_num)]
        return ExamParseResult(
            items=items,
            parsed_count=len(items),
            answer_key_count=len(answer_idx_by_num),
            answer_glyph_map={},
        )

    async def _extract_answer_key(self, answer_page: str) -> dict[int, int]:
        if not answer_page.strip():
            return {}
        raw = await self._llm.generate(
            _ANSWER_PROMPT.format(text=answer_page),
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=0.0,
            guided_json_schema=_ANSWER_SCHEMA,
        )
        parsed = _parse_json(raw) or {}
        out: dict[int, int] = {}
        for a in parsed.get("answers", []):
            num = a.get("number")
            idx = answer_label_to_index(a.get("answer"))
            if isinstance(num, int) and idx is not None:
                out[num] = idx
        return out

    def _chunks(self, text: str) -> list[str]:
        """문항 경계(`N.`)에서만 분할 — 한 문항이 청크 경계에서 잘리지 않게 한다.
        단순 char 분할은 문항 중간을 끊어 지문이 절단되므로(ADR-026 검증) 금지."""
        if len(text) <= self._max_chars:
            return [text]
        qstart = re.compile(r"^\s*\d{1,3}\.\s")
        chunks: list[str] = []
        cur: list[str] = []
        size = 0
        for ln in text.split("\n"):
            # 다음 문항 시작이고 현재 청크가 한도를 넘으면 그 경계에서 flush
            if qstart.match(ln) and cur and size + len(ln) > self._max_chars:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            chunks.append("\n".join(cur))
        return chunks
