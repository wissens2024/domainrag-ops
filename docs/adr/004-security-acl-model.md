# ADR-004: 보안 & ACL 모델

## Status
**Accepted** (2026-05-08)

> **2026-05-08 보완 노트 (ADR-008)**: 본 ADR의 ACL/SecurityLevel 모델은 단일 테넌트 가정이었음. ADR-008(Multi-Tenant Architecture) 도입으로 **`tenant_id`가 모든 ACL 검사의 1차 분기**가 된다 — 사용자가 동일 group/clearance를 가져도 다른 테넌트의 chunk에는 절대 도달할 수 없음. 격리 메커니즘: (1) Qdrant collection 이름 자체가 `chunks_<tenant_id>`로 분리, (2) PostgreSQL Row-Level Security가 `tenant_id` 일치 강제, (3) JWT claim과 URL path mirror가 인증 단계에서 mismatch 차단. 본 ADR의 SecurityLevel(PUBLIC/INTERNAL/CONFIDENTIAL/SECRET) cumulative 비교 시맨틱과 ACL list 매칭 규칙은 tenant 내부에서 그대로 유효. SecurityLevel 비교의 Qdrant payload 표현(int 정수화 vs match-any list)은 보안 축 후속 점검 시 결정.

---

## Context

### 문제: 폐쇄망 환경의 권한 관리

조직 내부 문서를 기반으로 하는 RAG 시스템에서:

- 👤 **일반 사용자**: 공개 문서만 접근
- 👥 **부서 직원**: 부서 문서 접근
- 🔒 **보안팀**: 기밀 문서 접근
- 🛡️ **관리자**: 모든 문서 접근

문제: 현재 RAG는 모든 문서에 동일하게 접근

```python
# ❌ 나쁜 예: 권한 없이 검색
def retrieve(question):
    chunks = qdrant.search(question, top_k=10)
    # 보안팀 전용 문서도 포함될 수 있음 ❌
    return chunks
```

### 요구사항

1. 사용자는 자신의 권한에 해당하는 문서만 검색
2. 권한 없는 문서는 검색 결과에 나타나지 않음
3. 문서 버전/유효 기간도 고려
4. 감사(Audit) 로그 기록

---

## Decision

### 보안 모델

#### 1. 보안 등급 (Security Level)

```python
class SecurityLevel(str, Enum):
    PUBLIC = "public"           # 누구나 접근
    INTERNAL = "internal"       # 조직 내부 (기본)
    CONFIDENTIAL = "confidential"  # 특정 부서/권한
    SECRET = "secret"           # 매우 제한적
```

#### 2. 사용자 Clearance (권한 등급)

```python
@dataclass
class UserContext:
    user_id: str
    groups: List[str]           # ["security", "it-admin", "finance"]
    clearance: str              # "public", "internal", "confidential"
    department: str             # "security", "it", "finance"
```

#### 3. ACL (Access Control List)

각 Chunk에 포함:

```python
@dataclass
class Chunk:
    chunk_id: str
    acl: List[str]              # ["group:security", "user:admin-001", "dept:it"]
    security_level: str         # "internal", "confidential"
    valid_from: Optional[date]  # 2026-05-01
    valid_until: Optional[date] # 2026-12-31 (null = 무제한)
    approval_status: str        # "draft", "approved", "archived"
```

### 접근 제어 로직

```python
def can_access_chunk(user: UserContext, chunk: Chunk) -> bool:
    """사용자가 chunk에 접근 가능한지 확인"""
    
    # 1. 보안 등급 체크
    if chunk.security_level > user.clearance:
        return False
    
    # 2. ACL 체크 (명시적으로 허용된 항목 있는지)
    if chunk.acl:
        has_access = (
            f"user:{user.user_id}" in chunk.acl or
            any(f"group:{g}" in chunk.acl for g in user.groups) or
            f"dept:{user.department}" in chunk.acl
        )
        if not has_access:
            return False
    
    # 3. 유효 기간 체크
    today = date.today()
    if chunk.valid_from and chunk.valid_from > today:
        return False
    if chunk.valid_until and chunk.valid_until < today:
        return False
    
    # 4. 승인 상태 체크
    if chunk.approval_status != "approved":
        return False
    
    return True
```

### Qdrant Payload Filter

Qdrant의 필터 기능으로 데이터베이스 단계에서 필터링:

```python
def build_acl_filter(user: UserContext) -> Filter:
    """Qdrant payload filter 생성"""
    
    # 보안 등급 필터
    level_filter = FieldCondition(
        key="security_level",
        match=MatchAny(any=[
            "public",
            "internal" if user.clearance >= "internal" else None,
            "confidential" if user.clearance >= "confidential" else None,
        ])
    )
    
    # ACL 필터
    acl_filter = FieldCondition(
        key="acl",
        match=MatchAny(any=[
            f"user:{user.user_id}",
            *[f"group:{g}" for g in user.groups],
            f"dept:{user.department}"
        ])
    )
    
    # 유효 기간 필터
    today = date.today()
    date_filter = Filter(
        must=[
            FieldCondition(key="valid_from", match=MatchValue(value=None)) or
                FieldCondition(key="valid_from", match=LessThanOrEqual(today)),
            FieldCondition(key="valid_until", match=MatchValue(value=None)) or
                FieldCondition(key="valid_until", match=GreaterThanOrEqual(today))
        ]
    )
    
    # 승인 상태 필터
    approval_filter = FieldCondition(
        key="approval_status",
        match=MatchValue(value="approved")
    )
    
    # 모든 필터 조합
    combined = Filter(
        must=[level_filter, approval_filter, date_filter],
        should=[acl_filter]  # ACL이 없으면 everyone이 볼 수 있음
    )
    
    return combined
```

### 사용: Retrieval with ACL

```python
def retrieve_with_acl(
    query: str,
    user_context: UserContext,
    top_k: int = 10
) -> List[Chunk]:
    """ACL을 적용하여 검색"""
    
    # 1. 질문 임베딩
    query_vector = embedder.embed(query)
    
    # 2. ACL 필터 생성
    acl_filter = build_acl_filter(user_context)
    
    # 3. Qdrant 검색 (필터 포함)
    results = qdrant.search(
        query_vector=query_vector,
        collection_name="chunks",
        query_filter=acl_filter,
        limit=top_k
    )
    
    # 4. 어플리케이션 레벨 재확인 (보안 강화)
    chunks = []
    for result in results:
        chunk = parse_chunk(result)
        if can_access_chunk(user_context, chunk):
            chunks.append(chunk)
    
    return chunks
```

### 예시: 검색 결과 비교

**사용자 A** (보안팀, clearance=confidential, groups=["security"])
```
질문: "반출 절차"

❌ 권한 없음:
- ["public"] → OK (보안 등급 맞음)
- ["internal"] → OK
- ["confidential"] → OK
- ["secret"] → ❌ clearance가 insufficient

❌ ACL 불일치:
- acl=["group:finance"] → ❌ finance 그룹이 아님
- acl=["group:security"] → OK (security 그룹 맞음)
- acl=[] → OK (제한 없음, 보안 등급만 체크)

❌ 유효 기간 문제:
- valid_from="2026-06-01" → ❌ 아직 시작 안 됨
- valid_until="2026-05-07" → ❌ 만료됨

검색 결과: 권한 있는 chunk만 반환
```

**사용자 B** (영업팀, clearance=public, groups=["sales"])
```
질문: "반출 절차"

❌ 권한 없음:
- ["public"] → OK
- ["internal"] → ❌ clearance가 insufficient
- ["confidential"] → ❌
- ["secret"] → ❌

검색 결과: public 문서만 반환
```

---

## Consequences

### ✅ 긍정적 영향

1. **데이터 보안**: 권한 없는 사용자는 기밀 정보 접근 불가
2. **컴플라이언스**: 조직의 보안 정책 준수
3. **감사**: 누가 어떤 정보에 접근했는지 추적 가능
4. **성능**: Qdrant 필터로 데이터베이스 레벨에서 최적화
5. **확장성**: 새로운 보안 정책 추가 용이

### ⚠️ 성능 고려사항

```python
# 필터가 있으면 검색 속도 약간 느려질 수 있음
# But: 보안이 성능보다 중요

# 최적화:
# 1. ACL을 작은 집합으로 유지
# 2. Qdrant Index를 잘 구성
# 3. 캐싱 활용 (사용자별 권한 캐시)
```

---

## Alternatives Considered

### 1. 검색 후 필터링 (Post-filtering)
```python
# ❌ 검토하지 않음
results = qdrant.search(query_vector, limit=100)
filtered = [r for r in results if can_access(user, r)]  # 너무 늦음
```
**문제**: 부족한 결과 (top_k 보장 안 함)

### 2. 사용자별 별도 인덱스
```python
# ❌ 검토하지 않음
# user-001 인덱스, user-002 인덱스, ...
```
**문제**: 인덱스 폭발적 증가

### 3. ✅ 현재 선택: 데이터베이스 레벨 필터
- 정확한 결과
- 좋은 성능
- 구현 간단

---

## 구현 체크리스트

- [ ] SecurityLevel Enum 정의
- [ ] UserContext 클래스 정의
- [ ] Chunk에 security_level, acl, valid_from, valid_until 추가
- [ ] can_access_chunk() 함수 구현
- [ ] build_acl_filter() 함수 구현
- [ ] retrieve_with_acl() 함수 구현
- [ ] Qdrant payload에 필터링 필드 포함
- [ ] 감사 로그 기록
- [ ] 보안 테스트 작성
- [ ] 문서화

---

## 감사 로그

모든 접근 시도 기록:

```python
@dataclass
class AuditLog:
    timestamp: datetime
    user_id: str
    action: str  # "search", "view", "download"
    resource: str  # doc_id, chunk_id
    allowed: bool
    reason: Optional[str]
```

---

## Related

- [ADR-001: Citation 메타데이터](./001-citation-metadata-design.md) - ACL이 포함된 메타데이터
- IMPLEMENTATION_SPEC.md §11 보안 설계 — 폐기 (본 ADR + [ADR-008](./008-multi-tenant-architecture.md) RLS로 흡수)
- [backend/README.md](../../backend/README.md#보안--acl) - 구현 가이드

---

**작성자**: AI Assistant  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성
