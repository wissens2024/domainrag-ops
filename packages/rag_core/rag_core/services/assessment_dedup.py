"""Assessment 중복 제거 (ADR-025 §5).

기출이 연도별로 재사용돼 동일 문항이 다수 누적된다(운영 실측: approved의 약 1/3이
중복 그룹에 묶임). 정확 일치(정규화된 question_text + 보기 집합) 기준으로 중복을
탐지한다. near-dup(의역) 탐지는 임베딩/Qdrant 기반(ADR-025 §5 검색 전략 전환)으로
별도 확장한다.

import 파이프라인이 신규 문항의 dedup_key가 기존에 있으면 skip(또는 출처 tag)하고,
기존 코퍼스 정리도 같은 key로 그룹핑한다 -- 단일 진실 소스 함수.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_NON_WORD = re.compile(r"[^0-9a-z가-힣]+")


def _norm_text(s: str) -> str:
    # NFKC로 전각/호환 문자 정규화 후, 영문 소문자화 + 단어 문자만 남긴다
    # (공백·구두점·보기 마커 차이로 인한 오탐 제거).
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return _NON_WORD.sub("", s)


def dedup_key(question_text: str, choices: list[Any]) -> str:
    """동일 문항 판별용 정규화 key.

    질문 본문 + 보기 *집합*(순서 무관)을 정규화해 결합한다. 보기 마커/공백/구두점
    차이로 인한 오탐을 줄이고, 보기 순서만 다른 같은 문항을 같은 key로 묶는다.
    """
    q = _norm_text(question_text)
    parts = sorted(_norm_text(str(c)) for c in (choices or []) if _norm_text(str(c)))
    return q + "|" + "|".join(parts)
