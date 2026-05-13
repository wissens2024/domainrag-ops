"""build_qdrant_acl_filter — ADR-004 §1·§2·§3·§4 정합성."""

from __future__ import annotations

from datetime import date

from qdrant_client import models

from rag_core.services.acl_builder import build_qdrant_acl_filter


def test_minimal_filter_without_dates():
    f = build_qdrant_acl_filter(
        user_id="u1",
        clearance="confidential",
        department="security",
        domain_groups=["group:security", "it-admin"],
    )
    assert f["must"][0] == {"key": "approval_status", "match": {"value": "approved"}}
    levels = f["must"][1]["match"]["any"]
    assert "public" in levels and "internal" in levels and "confidential" in levels
    assert "restricted" not in levels  # confidential 위는 차단

    acl_terms = f["must"][2]["match"]["any"]
    assert "user:u1" in acl_terms
    assert "dept:security" in acl_terms
    assert "group:security" in acl_terms
    # prefix 없는 항목은 group: 보강
    assert "group:it-admin" in acl_terms

    # today 미지정 → date 조건 없음
    assert len(f["must"]) == 3


def test_filter_with_today_includes_validity():
    f = build_qdrant_acl_filter(
        user_id="u1",
        clearance="internal",
        department=None,
        domain_groups=[],
        today=date(2026, 5, 9),
    )
    assert len(f["must"]) == 5
    # valid_from: should = [is_null, range lte today]
    valid_from_clause = f["must"][3]
    assert "should" in valid_from_clause
    assert {"is_null": {"key": "valid_from"}} in valid_from_clause["should"]
    range_cond = next(
        c for c in valid_from_clause["should"] if c.get("key") == "valid_from"
    )
    assert range_cond["range"]["lte"] == "2026-05-09"

    valid_until_clause = f["must"][4]
    range_cond_u = next(
        c for c in valid_until_clause["should"] if c.get("key") == "valid_until"
    )
    assert range_cond_u["range"]["gte"] == "2026-05-09"


def test_filter_validates_against_qdrant_filter_model():
    """결과가 qdrant_client.models.Filter pydantic으로 파싱 가능해야 한다."""
    f = build_qdrant_acl_filter(
        user_id="u1",
        clearance="confidential",
        department="security",
        domain_groups=["group:security"],
        today=date(2026, 5, 9),
    )
    parsed = models.Filter.model_validate(f)
    assert isinstance(parsed, models.Filter)
    assert parsed.must is not None and len(parsed.must) == 5


def test_clearance_unknown_falls_back_to_internal():
    f = build_qdrant_acl_filter(
        user_id="u1",
        clearance="not-a-real-level",
        department=None,
        domain_groups=[],
    )
    levels = f["must"][1]["match"]["any"]
    assert "public" in levels and "internal" in levels
    assert "confidential" not in levels


def test_exclude_archived_default_appends_must_not():
    """ADR-012 §3 — default로 must_not에 archived=true 제외."""
    f = build_qdrant_acl_filter(
        user_id="u1",
        clearance="internal",
        department=None,
        domain_groups=[],
    )
    assert f["must_not"] == [
        {"key": "archived", "match": {"value": True}}
    ]


def test_exclude_archived_false_drops_must_not():
    """admin Citation Inspector 등 archive까지 보고 싶을 때 — must_not 제거."""
    f = build_qdrant_acl_filter(
        user_id="u1",
        clearance="internal",
        department=None,
        domain_groups=[],
        exclude_archived=False,
    )
    assert "must_not" not in f


def test_exclude_archived_validates_against_qdrant_filter_model():
    """must_not 절도 Qdrant pydantic Filter 모델로 파싱 가능해야 한다."""
    f = build_qdrant_acl_filter(
        user_id="u1",
        clearance="confidential",
        department="security",
        domain_groups=["group:security"],
        today=date(2026, 5, 9),
    )
    parsed = models.Filter.model_validate(f)
    assert parsed.must_not is not None
    assert len(parsed.must_not) == 1
