"""ExamPaperParser — 기출 시험지 텍스트를 구조화 item으로 파싱 (ADR-025 §2).

순수 함수형 — PDF 라이브러리(fitz 등)에 의존하지 않는다. 입력은 *페이지별 텍스트*와
정답표 페이지 인덱스이며, 출력은 `ParsedExamItem` 목록이다. 그림 추출·문항-그림 연결은
좌표 정보가 필요하므로 fitz 추출 어댑터·import 서비스(Phase 2b/2c)가 담당하고, 본 파서는
*텍스트→문항/보기/정답* 변환만 책임진다.

표준 레이아웃 가정(에듀온류 4지선다 + 말미 정답표):
  - 과목 헤더: `【N과목】 과목명`
  - 문항: 줄 시작 `N.` (전역 1..total 순번)
  - 보기: `①②③④`
  - 정답표(마지막 페이지): 과목별 문항번호 블록 + 정답 글리프 블록
정답 글리프(①②③④ 또는 ㉮㉯㉰㉱)는 코드포인트 정렬로 1..4에 매핑한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 정보처리기사 필기 5과목 — header_no → subject enum (exam-engineer input_schema 정합).
# 출처별 편차가 있으므로 caller(configs)가 교체 가능. 본 상수는 표준 어댑터 기본값.
DEFAULT_GISA_SUBJECT_MAP: dict[str, str] = {
    "1": "software_design",
    "2": "software_dev",
    "3": "database",
    "4": "programming",
    "5": "info_system",
}

# 페이지 머리말/꼬리말 등 본문이 아닌 잡음 토큰 (줄에 포함되면 제거 대상).
DEFAULT_NOISE_TOKENS: tuple[str, ...] = (
    "eduon", "지식충전소", "수험번호", "자격종목", "시험시간", "감독위원",
    "OMR", "마킹", "수험자", "★", "비번호", "문제지", "답안카드", "성명",
    "응시", "※",
)

# 본문 줄 끝에 붙어 누출되는 페이지 푸터(국가기술자격 …형 별) 블록.
_FOOTER_RE = re.compile(
    r"국가기술자격\s*필기시험문제\d{0,4}년?도?\s*기사\s*제\d+회\s*필기시험\(기사\)정보처리기사형\s*별"
)
_SUBJECT_HEADER_RE = re.compile(r"^【(\d+)과목】\s*(.*)$")
_QUESTION_RE = re.compile(r"^(\d+)\.\s*(.*)$")
_CIRCLED = {"①": 0, "②": 1, "③": 2, "④": 3}


@dataclass
class ParsedExamItem:
    """파싱된 문항 1건. figure_refs는 import 서비스가 좌표 기반으로 채운다."""

    number: int
    subject: str | None
    subject_label: str | None
    question_text: str
    choices: list[str]
    answer_index: int | None  # 0-based
    answer_value: str | None
    figure_refs: list[str] = field(default_factory=list)
    figure_dependent: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class ExamParseResult:
    items: list[ParsedExamItem] = field(default_factory=list)
    parsed_count: int = 0
    answer_key_count: int = 0
    answer_glyph_map: dict[str, int] = field(default_factory=dict)  # glyph → 1..4


class ExamPaperParser:
    def __init__(
        self,
        *,
        subject_map: dict[str, str] | None = None,
        noise_tokens: tuple[str, ...] = DEFAULT_NOISE_TOKENS,
    ) -> None:
        self._subject_map = subject_map or DEFAULT_GISA_SUBJECT_MAP
        self._noise = noise_tokens

    # ------------------------------------------------------------------ #
    def parse(
        self,
        *,
        page_texts: list[str],
        answer_page_index: int | None = None,
    ) -> ExamParseResult:
        if not page_texts:
            return ExamParseResult()
        ans_idx = answer_page_index if answer_page_index is not None else len(page_texts) - 1
        answer_page = page_texts[ans_idx]
        question_pages = [t for i, t in enumerate(page_texts) if i != ans_idx]

        answers_glyph, glyph_map = self._parse_answer_key(answer_page)
        answers_idx = {n: glyph_map[g] for n, g in answers_glyph.items()}

        items = self._parse_questions(question_pages, answers_idx, answers_glyph)
        return ExamParseResult(
            items=items,
            parsed_count=len(items),
            answer_key_count=len(answers_idx),
            answer_glyph_map={g: i + 1 for g, i in glyph_map.items()},
        )

    # ------------------------------------------------------------------ #
    def _parse_answer_key(
        self, answer_page: str
    ) -> tuple[dict[int, str], dict[str, int]]:
        """정답표 → {문항번호: 글리프}, {글리프: 0-based index}.

        구조: [10개 번호][10개 글리프] 반복. 번호↔글리프는 위치로 정렬.
        """
        seq: list[tuple[str, object]] = []
        for ln in (l.strip() for l in answer_page.split("\n")):
            if not ln:
                continue
            if re.fullmatch(r"\d+", ln):
                seq.append(("num", int(ln)))
            elif len(ln) == 1 and not ln.isascii():
                seq.append(("ans", ln))

        answers: dict[int, str] = {}
        buf_nums: list[int] = []
        buf_ans: list[str] = []
        for typ, val in seq:
            if typ == "num":
                if buf_ans:  # 블록 경계 — 직전 블록 확정 후 리셋
                    for n, a in zip(buf_nums, buf_ans):
                        answers[n] = a
                    buf_nums, buf_ans = [], []
                buf_nums.append(val)  # type: ignore[arg-type]
            else:
                buf_ans.append(val)  # type: ignore[arg-type]
        for n, a in zip(buf_nums, buf_ans):
            answers[n] = a

        distinct = sorted(set(answers.values()), key=ord)
        glyph_map = {g: i for i, g in enumerate(distinct)}
        return answers, glyph_map

    # ------------------------------------------------------------------ #
    def _clean_lines(self, question_pages: list[str]) -> list[str]:
        raw = _FOOTER_RE.sub("", "\n".join(question_pages))
        # 한 줄에 보기 마커가 2개 이상 붙은 경우 비-선두 마커 앞에서 줄 분리
        raw = re.sub(r"(?<=\S)\s*([①②③④])", r"\n\1", raw)
        out: list[str] = []
        for ln in raw.split("\n"):
            # 줄바꿈 경계 공백 보존 — PDF는 어절 경계 줄바꿈에 trailing space를 넣고
            # 단어 중간 분리엔 넣지 않는다. trailing을 strip하면 "설명으로틀린"처럼 붙으므로
            # leading만 제거(lstrip)하고 trailing space는 유지해 연속 줄 join 시 복원한다.
            content = ln.lstrip()
            if not content.strip() or any(tok in content for tok in self._noise):
                continue
            out.append(content)
        return out

    def _parse_questions(
        self,
        question_pages: list[str],
        answers_idx: dict[int, int],
        answers_glyph: dict[int, str],
    ) -> list[ParsedExamItem]:
        clean = self._clean_lines(question_pages)

        raw_items: list[dict] = []
        cur: dict | None = None
        state: object = None  # "q" | int(choice idx)
        cur_subj: str | None = None
        expected = 1

        def push(s: str) -> None:
            if state == "q":
                cur["q"].append(s)  # type: ignore[index]
            elif isinstance(state, int):
                cur["choices"][state].append(s)  # type: ignore[index]

        for ln in clean:
            mh = _SUBJECT_HEADER_RE.match(ln)
            if mh:
                cur_subj = mh.group(1)
                continue
            mq = _QUESTION_RE.match(ln)
            if mq and int(mq.group(1)) == expected:
                if cur:
                    raw_items.append(cur)
                cur = {
                    "number": expected,
                    "subject_no": cur_subj,
                    "q": [mq.group(2)] if mq.group(2) else [],
                    "choices": [[], [], [], []],
                }
                state = "q"
                expected += 1
                continue
            if ln[0] in _CIRCLED:
                idx = _CIRCLED[ln[0]]
                state = idx
                rest = ln[1:].lstrip()  # 마커 뒤 공백만 제거, trailing은 보존
                if rest:
                    cur["choices"][idx].append(rest)  # type: ignore[index]
                continue
            if cur is not None:
                push(ln)
        if cur:
            raw_items.append(cur)

        return [self._assemble(r, answers_idx, answers_glyph) for r in raw_items]

    # ------------------------------------------------------------------ #
    def _assemble(
        self,
        r: dict,
        answers_idx: dict[int, int],
        answers_glyph: dict[int, str],
    ) -> ParsedExamItem:
        n = r["number"]
        subj_no = r["subject_no"]
        enum = self._subject_map.get(subj_no) if subj_no else None
        qtext = "".join(r["q"]).strip()
        choices = ["".join(c).strip() for c in r["choices"]]
        nonempty = [c for c in choices if c]
        ai = answers_idx.get(n)

        flags: list[str] = []
        if len(nonempty) != 4:
            flags.append(f"choices={len(nonempty)}")
        if len(qtext) < 8:
            flags.append("question_text_short")
        if ai is None:
            flags.append("no_answer")
        blob = qtext + " " + " ".join(nonempty)
        if any(t in qtext for t in ("그림", "도표", "다음 표", "보기와 같")):
            flags.append("figure_dependent")
        if any(
            t in blob
            for t in (";", "{", "}", "printf", "#include", "public ", "void ",
                      "int ", "class ", "SELECT", "CREATE", "static ", "return ",
                      "System.out", "def ")
        ):
            flags.append("code_or_sql")

        answer_value = nonempty[ai] if (ai is not None and len(nonempty) == 4) else None
        return ParsedExamItem(
            number=n,
            subject=enum,
            subject_label=subj_no,
            question_text=qtext,
            choices=nonempty if len(nonempty) == 4 else choices,
            answer_index=ai,
            answer_value=answer_value,
            figure_dependent="figure_dependent" in flags,
            flags=flags,
        )
