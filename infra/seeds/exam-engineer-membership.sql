-- ============================================================================
-- exam-engineer tenant — user_tenant_membership 시드
-- ----------------------------------------------------------------------------
-- 실행 전 AuthFusion에서 cs + WiSentinel dev 사용자의 sub(UUID)을 받아 placeholder를
-- 실 UUID로 교체. 절차: docs/operations/exam-engineer-onboarding.md §2.4.
--
-- 실행 role: domainrag_platform_admin (BYPASSRLS). RLS 영향 없음 — tenant_id가 명시.
-- 검증: SELECT email, clearance, department FROM user_tenant_membership
--       WHERE tenant_id='exam-engineer' AND is_active=true;
-- ============================================================================

BEGIN;

-- 1) cs@wissensbaum.com — DomainRAG admin (confidential clearance)
INSERT INTO user_tenant_membership (
    user_id, tenant_id, clearance, department, domain_groups,
    preferred_username, email, is_active, created_at
) VALUES (
    '<TODO: cs sub UUID from AuthFusion POST /api/v1/users response>',
    'exam-engineer',
    'confidential',
    'customer-success',
    ARRAY['group:exam-engineer-admin', 'group:wissens-internal']::text[],
    'cs',
    'cs@wissensbaum.com',
    true,
    NOW()
)
ON CONFLICT (user_id, tenant_id) DO UPDATE SET
    clearance = EXCLUDED.clearance,
    department = EXCLUDED.department,
    domain_groups = EXCLUDED.domain_groups,
    is_active = true;

-- 2) WiSentinel AI dev 부서 — DomainRAG user (internal clearance)
-- AuthFusion에서 사용자별 sub UUID 조회 후 채울 것:
--   curl -s "https://console.aines.kr/api/v1/users?email=<email>" \
--     -H "Authorization: Bearer $TOKEN" | jq -r '.[0].id'
INSERT INTO user_tenant_membership (
    user_id, tenant_id, clearance, department, domain_groups,
    preferred_username, email, is_active, created_at
) VALUES
    ('<TODO: alice sub UUID>',   'exam-engineer', 'internal', 'ai-dev',
     ARRAY['group:wisentinel-dev']::text[], 'alice',   'alice@example.com',   true, NOW()),
    ('<TODO: bob sub UUID>',     'exam-engineer', 'internal', 'ai-dev',
     ARRAY['group:wisentinel-dev']::text[], 'bob',     'bob@example.com',     true, NOW()),
    ('<TODO: charlie sub UUID>', 'exam-engineer', 'internal', 'ai-dev',
     ARRAY['group:wisentinel-dev']::text[], 'charlie', 'charlie@example.com', true, NOW())
ON CONFLICT (user_id, tenant_id) DO UPDATE SET
    clearance = EXCLUDED.clearance,
    department = EXCLUDED.department,
    is_active = true;

-- 3) 명시 부여 — admin 자신도 자기 tenant chat 사용 가능해야 함.
-- (clearance=confidential이면 모든 chunk 접근 가능. user_tenant_membership 단일 row로 충분.)

COMMIT;

-- ----------------------------------------------------------------------------
-- Rollback (사용자 제거 시):
--   UPDATE user_tenant_membership
--      SET is_active = false, deactivated_at = NOW()
--    WHERE tenant_id = 'exam-engineer' AND email = '<email>';
-- DELETE는 audit 흔적 손실 — soft-delete 권장. ADR-020 §10.
-- ----------------------------------------------------------------------------
