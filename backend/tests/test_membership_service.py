"""InMemoryMembershipService — assign/list/revoke (ADR-022 §3·§4)."""

from __future__ import annotations

import pytest

from app.services.membership_service import InMemoryMembershipService


@pytest.mark.asyncio
async def test_assign_then_list_for_user_and_tenant():
    svc = InMemoryMembershipService()
    await svc.assign(
        user_id="u1", domain_id="security", clearance="confidential",
        department="sec", domain_groups=["group:sec"],
    )
    await svc.assign(user_id="u1", domain_id="legal")
    await svc.assign(user_id="u2", domain_id="security")

    by_user = await svc.list_for_user("u1")
    assert {m["domain_id"] for m in by_user} == {"security", "legal"}
    sec = next(m for m in by_user if m["domain_id"] == "security")
    assert sec["clearance"] == "confidential"
    assert sec["domain_groups"] == ["group:sec"]

    by_tenant = await svc.list_for_tenant("security")
    assert {m["user_id"] for m in by_tenant} == {"u1", "u2"}


@pytest.mark.asyncio
async def test_assign_is_upsert():
    svc = InMemoryMembershipService()
    await svc.assign(user_id="u1", domain_id="security", clearance="internal")
    await svc.assign(user_id="u1", domain_id="security", clearance="secret")
    rows = await svc.list_for_user("u1")
    assert len(rows) == 1
    assert rows[0]["clearance"] == "secret"


@pytest.mark.asyncio
async def test_revoke_deactivates_and_hides_from_user_list():
    svc = InMemoryMembershipService()
    await svc.assign(user_id="u1", domain_id="security")
    assert await svc.revoke(user_id="u1", domain_id="security") is True
    assert await svc.list_for_user("u1") == []
    # 두 번째 revoke는 이미 비활성 → False
    assert await svc.revoke(user_id="u1", domain_id="security") is False


@pytest.mark.asyncio
async def test_revoke_missing_returns_false():
    svc = InMemoryMembershipService()
    assert await svc.revoke(user_id="ghost", domain_id="security") is False
