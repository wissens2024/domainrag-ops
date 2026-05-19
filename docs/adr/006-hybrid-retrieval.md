# ADR-006: Hybrid Retrieval (bge-m3 Dense + Sparse, Qdrant DBSF)

## Status
**Superseded by ADR-011** (2026-05-08)

> **본 ADR은 [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md)에 의해 supersede됨.** ADR-011이 본 ADR의 모든 결정(bge-m3 dense+sparse, named vectors, DBSF fusion, top_k 단계)을 흡수하고 multi-tenant 운영(collection-per-tenant, 임베딩 이종화) + 이전에 deferred 처리된 항목들(query rewriting 알고리즘 완전 구현, reranker per-tenant override, 가용성 실패 핸들링)을 완전 설계로 추가한다. **단일 진실 소스는 ADR-011**.
>
> 본 ADR은 historical reference로 보존되며, 신규 코드/SPEC 갱신은 ADR-011을 인용해야 한다.

---

## Context

### 배경 (사실)

- SPEC.md v1 §4 아키텍처 그림에 "Sparse/BM25 Search" 박스가 있으나 §10 `retrieve_context` 노드 사양은 dense search만 언급. **결합 사양이 어디에도 없음**.
- ADR-005 Gate 1 (`rerank_score > 0.5인 chunk 수 < 2 OR top1 < 0.6 → fallback`)이 retrieve 단계 신호 신뢰성을 전제로 함.
- 도메인 특성: 보안 매뉴얼·지침·반출 절차 — **정확 키워드(법규 조항·절차명)** 와 **개념적 질의** 두 신호 모두 요구.
- 폐쇄망 환경 — 추가 인프라 도입 부담 최소화 필요.

### 가정

- Qdrant 1.10+ 설치 가능 (named vectors + Query API + DBSF fusion)
- bge-m3가 dense(1024차원) + sparse 두 신호를 모두 출력함 (모델 사양상 검증됨)
- 평가 셋(`data/eval/domain_qa.jsonl`)이 가중치/top_k 튜닝에 사용 가능

가정 중 하나라도 깨지면 본 ADR 재검토. 특히 Qdrant 1.10+ 미만 운영 강제 시 application-level weighted sum으로 우회 필요.

---

## Decision

### 1. Dense + Sparse 두 신호, bge-m3 단일 모델 책임
- chunk indexing과 query 모두 bge-m3로 dense·sparse 출력
- 별도 한국어 토크나이저(Kiwi 등) 도입 불필요. Kiwi는 ADR-005의 Tier 3·heuristic claim 분리에만 잔존

### 2. Qdrant collection 통합 — Named Vectors
```python
# collection 스키마 (예시)
{
    "vectors": {"dense": {"size": 1024, "distance": "Cosine"}},
    "sparse_vectors": {"sparse": {}}
}
```
같은 point에 dense·sparse 두 named vector가 보관됨 → chunk 단위 데이터 일관성 자동 보장.

### 3. Fusion — Qdrant DBSF (Distribution-Based Score Fusion)
```python
# Qdrant Query API
client.query_points(
    collection_name="chunks",
    prefetch=[
        {"using": "dense",  "query": dense_q,  "limit": 30},
        {"using": "sparse", "query": sparse_q, "limit": 30},
    ],
    query=FusionQuery(fusion=Fusion.DBSF),
    limit=50,
)
```
가중치 시작값 `α(dense) = β(sparse) = 0.5`. `configs/retrieval.yaml`로 분리, 평가 셋으로 튜닝.

### 4. top_k 단계
| 단계 | 값 | 비고 |
|---|---|---|
| dense_top_k | 30 | prefetch |
| sparse_top_k | 30 | prefetch |
| fused_top_k | 50 | DBSF 결과 |
| rerank_top_k | 10 | cross-encoder 재정렬 후 |
| context_top_k | 5 | LLM에 전달 |

ADR-005 Gate 1의 `strong chunks ≥ 2`에 충분한 후보를 보장.

### 5. Query Rewriting — MVP 미도입
- LangGraph state의 `rewritten_query` 필드는 schema에 두되 v1에서는 `question` 동일값 입력
- LLM-based query expansion(HyDE 등)은 Phase 2에서 평가 후 도입 검토

### 6. retrieve_context 노드 책임 (SPEC §10.2 갱신)
```text
1) ACL filter 생성 (ADR-004)
2) bge-m3로 question을 dense+sparse 임베딩 생성
3) Qdrant Query API: prefetch dense+sparse → DBSF fusion → fused_top_k 반환
4) ACL filter는 prefetch 단계와 fusion 단계 모두에 적용 (Qdrant payload filter)
5) 반환에는 dense_score, sparse_score, fused_score 모두 포함 (chat_logs 추적용)
```

### 7. configs/retrieval.yaml (신설)
```yaml
hybrid:
  fusion: dbsf                # dbsf | rrf | weighted_sum
  weights:
    dense: 0.5
    sparse: 0.5
top_k:
  dense: 30
  sparse: 30
  fused: 50
  rerank: 10
  context: 5
query_rewriting:
  enable: false               # MVP 미도입
  strategy: null              # null | hyde | llm_expand
embedding:
  model: bge-m3
  dense_dim: 1024
```

---

## Consequences

### 긍정적 영향
- 추가 인프라 0 (OpenSearch 등 도입 회피)
- 토크나이저 일관성 자동 (bge-m3가 양 신호 책임)
- 임베딩 모델 교체 시 dense·sparse 동시 재생성 — 마이그레이션 부담 단일화 (ADR-007 ③-4)
- 메타데이터-only 갱신 시 Qdrant `set_payload` 한 번 (이중 시스템 동기화 회피)
- DBSF로 score 정규화 부담을 Qdrant에 위임

### 부정적 영향 / 부채
- Qdrant 1.10+ 강제 의존
- DBSF 내부 동작에 의존 — fusion 디버깅 시 Qdrant 로그·문서에 의존
- Query rewriting 미도입 — paraphrase 변형 질의 약점 (Phase 2)
- 가중치 튜닝은 평가 셋 구축 후에야 의미 — 시작값은 휴리스틱

### 후속 작업
- `configs/retrieval.yaml` 신설
- SPEC §4 다이어그램의 "Sparse/BM25" → "bge-m3 Sparse (Qdrant named vector)" 갱신
- SPEC §10.2 retrieve_context 노드 사양 갱신
- chunks indexing 시 bge-m3 dense+sparse 동시 출력 → Qdrant upsert
- Phase 2 후보: query rewriting 효과 평가, weighted_sum 전환 여부, 동적 가중치(query type 기반)

---

## Alternatives Considered

### 1. Pure Dense Search (sparse 미도입)
- **장점**: 인프라 단순
- **기각 사유**: 정확 키워드(법규·절차명·문서 번호) 검색 약함. 도메인 특성과 부합 안 함.

### 2. PostgreSQL tsvector
- **장점**: 추가 인프라 0
- **기각 사유**: 한국어 분석기 약함, 별도 형태소 분석 wrapping 필요. dense와 별도 시스템.

### 3. OpenSearch (nori 한국어 분석기)
- **장점**: BM25·한국어 분석 성숙, 운영 노하우 풍부
- **기각 사유**: 인프라 +1 컨테이너, dual-write 동기화 부담, 폐쇄망 운영 부담 가중.

### 4. Qdrant sparse + Kiwi로 BM25 직접 계산
- **장점**: 토크나이저 자유도 ↑
- **기각 사유**: bge-m3 sparse 미사용, 토크나이저 일관성을 우리가 책임, 모델 교체 시 sparse 재생성 비용 별도.

### 5. RRF (Reciprocal Rank Fusion)
- **장점**: score 정규화 회피, 이론적 robust
- **기각 사유**: rank-only로 가중 조정 자유도 낮음. 도메인 특성에 따라 dense/sparse 비중 조정 필요 시 한계.

### 6. Application-level Weighted Sum
- **장점**: 정규화 알고리즘 자유도 ↑, 디버깅 trace 쉬움
- **기각 사유**: 정규화 구현 우리 책임, dense+sparse 두 번 query 후 합성 코드 추가, 운영 단순성 ↓.

---

## Related
- [ADR-002: Protocol/Adapter 패턴](./002-protocol-adapter-pattern.md) — Retriever/VectorStore protocol
- [ADR-004: 보안 & ACL 모델](./004-security-acl-model.md) — ACL filter는 prefetch 단계에 적용
- [ADR-005: Citation 신뢰 모델](./005-citation-trust-and-fallback.md) — Gate 1 임계치는 rerank_score 기준 (fusion 방식과 독립)
- [ADR-007: Document/Chunk Lifecycle](./007-document-chunk-lifecycle.md) — 임베딩 모델 교체 시 collection swap 정책
- SPEC.md §4, §10.2 — 폐기 (본 ADR로 갱신 후 [ADR-011](./011-hybrid-retrieval-v2.md)이 supersede)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08  
**변경 이력**: 초안 작성, 즉시 Accepted
