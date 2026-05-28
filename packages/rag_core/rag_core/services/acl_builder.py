"""build_qdrant_acl_filter — ADR-004 §접근 제어 로직을 Qdrant payload filter dict로 변환.

규칙 (ADR-004 §1·§2·§3·§4):
  1. security_level: cumulative — chunk.security_level의 rank ≤ user.clearance rank
  2. acl 교집합: chunk.acl 배열에 user의 식별자(user/group/dept) 중 하나라도 포함
  3. 유효 기간: today ∈ [valid_from, valid_until], null은 무제한
  4. approval_status == 'approved'

본 함수는 pure function — DB·Qdrant 호출 없음. 결과 dict는 qdrant_client.models.Filter
pydantic 모델로 validate 가능한 형태.

ADR-008·018: domain_id는 collection 이름(`chunks_<domain_id>`)으로 1차 격리되므로
본 filter에는 domain_id 조건을 포함하지 않는다 (RLS·collection 격리가 안전망).
"""

from __future__ import annotations

from datetime import date

# ADR-004 §1 SecurityLevel cumulative 순서
_SECURITY_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
    "secret": 3,  # 별칭
}


def _allowed_levels(clearance: str) -> list[str]:
    """clearance 이하의 모든 보안 등급 (ADR-004 §1)."""
    user_rank = _SECURITY_RANK.get(clearance, _SECURITY_RANK["internal"])
    return [lvl for lvl, rank in _SECURITY_RANK.items() if rank <= user_rank]


def _user_acl_terms(
    user_id: str,
    department: str | None,
    domain_groups: list[str],
) -> list[str]:
    """ADR-004 §3 ACL list와 매칭되는 사용자 식별자 집합."""
    terms: list[str] = [f"user:{user_id}"]
    if department:
        terms.append(f"dept:{department}")
    # domain_groups는 보통 "group:security" 형식. prefix 없는 항목도 group:로 보강.
    for g in domain_groups:
        terms.append(g if g.startswith(("user:", "group:", "dept:")) else f"group:{g}")
    # 중복 제거 + 결정론적 순서
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def build_qdrant_acl_filter(
    *,
    user_id: str,
    clearance: str,
    department: str | None,
    domain_groups: list[str],
    today: date | None = None,
    exclude_archived: bool = True,
) -> dict:
    """ADR-004 §접근 제어 로직 1·2·3·4 + ADR-012 §3 archived 제외 필터.

    Args:
        today: None이면 유효 기간 필터를 생략 (테스트용). 운영은 항상 date.today() 전달.
        exclude_archived: True면 must_not에 `archived=True`를 추가해 archival_worker가
            마킹한 chunk를 검색에서 제외 (ADR-012 §3). 운영 default True. admin Citation
            Inspector 등 archive까지 보고 싶은 흐름은 False로 호출.
    """
    must: list[dict] = [
        {"key": "approval_status", "match": {"value": "approved"}},
        {"key": "security_level", "match": {"any": _allowed_levels(clearance)}},
        {
            "key": "acl",
            "match": {"any": _user_acl_terms(user_id, department, domain_groups)},
        },
    ]
    if today is not None:
        today_iso = today.isoformat()
        # valid_from: null OR <= today
        must.append(
            {
                "should": [
                    {"is_null": {"key": "valid_from"}},
                    {"key": "valid_from", "range": {"lte": today_iso}},
                ]
            }
        )
        # valid_until: null OR >= today
        must.append(
            {
                "should": [
                    {"is_null": {"key": "valid_until"}},
                    {"key": "valid_until", "range": {"gte": today_iso}},
                ]
            }
        )

    filter_dict: dict = {"must": must}
    if exclude_archived:
        # archival_worker가 set_payload({"archived": True})한 chunk만 제외.
        # archive되지 않은 chunk는 payload에 archived 키 자체가 없거나 false — 조건 불일치.
        filter_dict["must_not"] = [
            {"key": "archived", "match": {"value": True}}
        ]
    return filter_dict
