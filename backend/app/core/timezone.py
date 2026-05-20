"""KST timezone 헬퍼 (CLAUDE.md Y4).

API 응답·log에 노출되는 모든 timestamp는 KST(+09:00) isoformat으로 직렬화한다.
postgres TIMESTAMPTZ는 internally UTC instant로 저장되지만 client(asyncpg)가
UTC tzinfo로 normalize하므로 응답 직전 명시적 변환 필요.

UTC 누출(`+00:00`이 사용자 화면에 보임)을 원천 차단해 운영자 가독성 + CC/GS
인증 시 audit log 시각 일관성 확보.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), name="Asia/Seoul")


def iso_kst(dt: datetime | date | None) -> str | None:
    """datetime/date → KST isoformat string (None safe).

    - None: None 반환
    - timezone-naive datetime: KST tz로 간주 (운영 default)
    - timezone-aware datetime: KST로 변환
    - date(시간 없음): isoformat 그대로 (YYYY-MM-DD)
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=KST).isoformat()
        return dt.astimezone(KST).isoformat()
    # date instance — 시간 없음
    return dt.isoformat()


def now_kst() -> datetime:
    """현재 시각 (KST tz-aware)."""
    return datetime.now(KST)


def now_kst_iso() -> str:
    """현재 시각 KST isoformat string."""
    return now_kst().isoformat()
