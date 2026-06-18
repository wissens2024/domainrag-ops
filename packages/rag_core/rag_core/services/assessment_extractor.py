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


_GRID_NUM_RE = re.compile(r"\d{1,3}")


def parse_transposed_answer_grid(page_texts: list[str]) -> dict[int, int]:
    """세로 전치(transposed) 그리드형 정답표를 결정론적으로 파싱한다 (ADR-026 §3 보강).

    상당수 기출 PDF의 정답표는 LLM이 읽기 어려운 형태다: 문항 번호 N개가 한 줄에
    하나씩 세로로 나열된 뒤, 같은 개수의 정답이 다시 세로로 이어진다. 정답 표기는
    ①②③④ / 1~4 / 가나다라가 혼재하며, 특히 '맨숫자(1~4)' 정답은 문항 번호와 글자가
    같아 LLM이 번호와 정답을 구분하지 못해 통째로 실패한다(운영 검증: 해당 PDF appr=0).

    구조를 이용해 복구한다: 오름차순 연속 정수 run을 '문항 번호 블록'으로 보고, 바로
    뒤따르는 같은 길이의 블록을 '정답 블록'으로 페어링한다. 정답 토큰은 라벨(①/1/가)이
    거나 맨숫자(1~4)다. 모호한 매칭은 버린다.

    Returns:
        {문항 번호: 0-based 정답 인덱스}. 그리드를 못 찾으면 빈 dict.
    """
    # 정답 영역 외 본문에서 단독 정수/라벨 줄은 드물다(본문 번호는 "1." 처럼 점이 붙고,
    # 보기 마커는 텍스트에 붙어 있다). 따라서 단독 줄만 토큰화하면 오검출이 낮다.
    tokens: list[tuple[str, int]] = []  # ("num", n) | ("ans", idx)
    for text in page_texts:
        for line in text.split("\n"):
            s = line.strip()
            if _GRID_NUM_RE.fullmatch(s):
                tokens.append(("num", int(s)))
            elif s in _LABEL_TO_IDX:
                tokens.append(("ans", _LABEL_TO_IDX[s]))
    out: dict[int, int] = {}
    i, n = 0, len(tokens)
    while i < n:
        if tokens[i][0] != "num":
            i += 1
            continue
        # 오름차순으로 연속(+1)되는 정수 run = 문항 번호 블록
        j = i
        while j + 1 < n and tokens[j + 1][0] == "num" and tokens[j + 1][1] == tokens[j][1] + 1:
            j += 1
        length = j - i + 1
        if length >= 3 and i + 2 * length <= n and tokens[i][1] <= 100:
            block = tokens[i + length : i + 2 * length]
            vals: list[int] = []
            ok = True
            for kind, val in block:
                if kind == "ans":
                    vals.append(val)
                elif kind == "num" and 1 <= val <= 4:  # 맨숫자 정답(1~4)
                    vals.append(val - 1)
                else:  # 정답으로 볼 수 없는 토큰 → 이 블록은 정답표가 아님
                    ok = False
                    break
            if ok:
                for num, idx in zip(
                    (t[1] for t in tokens[i : i + length]), vals
                ):
                    out[num] = idx
                i += 2 * length
                continue
        i += 1
    return out


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


_HANGUL_RE = re.compile(r"[가-힣]")
_HAN_RE = re.compile(r"[一-鿿]")


def _script_counts(text: str) -> tuple[int, int]:
    """(한글 음절 수, CJK 한자 수)."""
    return len(_HANGUL_RE.findall(text or "")), len(_HAN_RE.findall(text or ""))


def detect_language_drift(question_text: str, *, source_is_hangul: bool) -> bool:
    """원문이 한글 위주인데 추출 문항이 CJK 한자 위주면 '번역 드리프트'로 판정.

    Qwen 등 중국어 사전학습 LLM이 한국어 문항을 중국어로 번역·치환하는 현상(통제실험
    확인). 프롬프트의 언어 고정으로 대부분 막지만 잔여가 있어, 드리프트 문항이
    auto_approve로 새지 않도록 감지해 import가 검수로 보낸다. 한국어 본문에 흔한 소수
    한자(괄호 속 한자어 등)를 오탐하지 않도록 '한자 우세 + 한글 빈약'을 함께 본다.
    """
    if not source_is_hangul:
        return False
    hangul, han = _script_counts(question_text)
    return han >= 3 and han > hangul


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
    # 언어 고정: Qwen 등 중국어 사전학습 모델이 특정 한국어 청크에서 결정론적으로
    # 중국어로 번역·드리프트하는 것을 막는다(통제 실험으로 5/5→0/5 확인). 언어 무관
    # 원칙(ADR-026)을 위해 특정 언어를 박지 않고 '원문 언어 유지'로 표현한다.
    "**원문에 사용된 언어를 그대로 유지하라 — 다른 언어로 번역·치환하는 것을 절대 "
    "금지한다.** 원문 글자를 그대로 쓰되, 줄바꿈으로 끊긴 단어만 이어붙여라. "
    "없는 내용을 지어내지 마라.\n\n"
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
        max_tokens: int = 2200,
        max_questions_per_chunk: int = 4,  # 청크당 문항 수 제한 → JSON 출력 절단 방지
    ) -> None:
        self._llm = llm_client
        self._model = model
        self._max_chars = max_chars_per_call
        self._max_tokens = max_tokens
        self._max_q = max_questions_per_chunk

    async def _generate_json(self, prompt: str, schema: dict, retries: int = 1) -> dict | None:
        """LLM 호출 + JSON 파싱. 실패(timeout/파싱)는 None — 한 청크 실패가 PDF 전체를
        중단시키지 않게 한다(ADR-026 견고성). retries회 재시도."""
        for _ in range(retries + 1):
            try:
                raw = await self._llm.generate(
                    prompt,
                    model=self._model,
                    max_tokens=self._max_tokens,
                    temperature=0.0,
                    guided_json_schema=schema,
                )
            except Exception:  # noqa: BLE001 — timeout/연결 등 모두 흡수 후 재시도
                continue
            parsed = _parse_json(raw)
            if parsed is not None:
                return parsed
        return None

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

        # 원문 언어 판정(드리프트 감지 기준) — 한글 위주인지.
        src_hangul, src_han = _script_counts("\n".join(question_pages))
        source_is_hangul = src_hangul >= 20 and src_hangul > src_han

        # 1. 문항 추출 (청크 단위, 청크 실패는 건너뜀)
        items_by_num: dict[int, ParsedExamItem] = {}
        for chunk in self._chunks("\n".join(question_pages)):
            parsed = await self._generate_json(
                _QUESTION_PROMPT.format(chunk=chunk), _QUESTION_SCHEMA
            ) or {}
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

        # 2. 정답표 추출: 결정론적 전치 그리드 우선(ADR-026 §3) → 부족하면 LLM 보강.
        #    그리드는 공식 정답표를 그대로 읽으므로 LLM이 실패하는 전치형도 복구하고,
        #    충분히 커버되면 LLM 호출을 생략해 비용도 줄인다.
        grid = parse_transposed_answer_grid(page_texts)
        if items_by_num and len(grid) >= 0.8 * len(items_by_num):
            answer_idx_by_num = grid
        else:
            llm_key = await self._extract_answer_key(answer_page)
            # 그리드(공식 정답표)가 LLM 결과보다 우선
            answer_idx_by_num = {**llm_key, **grid}

        # 3. 정답 매핑 + 교차검증 (ADR-026 §3) + 언어 드리프트 표식
        for num, item in items_by_num.items():
            if detect_language_drift(item.question_text, source_is_hangul=source_is_hangul):
                item.flags.append("language_drift")
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
        parsed = await self._generate_json(
            _ANSWER_PROMPT.format(text=answer_page), _ANSWER_SCHEMA
        ) or {}
        out: dict[int, int] = {}
        for a in parsed.get("answers", []):
            num = a.get("number")
            idx = answer_label_to_index(a.get("answer"))
            if isinstance(num, int) and idx is not None:
                out[num] = idx
        return out

    def _chunks(self, text: str) -> list[str]:
        """문항 경계(`N.`)에서만 분할 — 한 문항이 청크 경계에서 잘리지 않게 한다.
        청크당 문항 수(max_q)·문자 수(max_chars) 둘 다 한도로 둬서 JSON 출력이
        max_tokens를 초과해 잘리는 것을 막는다(ADR-026 검증: 출력 절단이 문항 유실 원인)."""
        qstart = re.compile(r"^\s*\d{1,3}\.\s")
        chunks: list[str] = []
        cur: list[str] = []
        size = 0
        qcount = 0
        for ln in text.split("\n"):
            is_q = bool(qstart.match(ln))
            if is_q and cur and (qcount >= self._max_q or size + len(ln) > self._max_chars):
                chunks.append("\n".join(cur))
                cur, size, qcount = [], 0, 0
            if is_q:
                qcount += 1
            cur.append(ln)
            size += len(ln) + 1
        if cur:
            chunks.append("\n".join(cur))
        return chunks or [text]
