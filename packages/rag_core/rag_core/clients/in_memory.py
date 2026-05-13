"""InMemory mock 구현체 — 테스트·로컬 개발용.

Protocol 계약을 충족하지만 외부 의존이 없다 (httpx, qdrant 모두 미사용).
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..interfaces.chunk_repository import (
    ChunkRecord,
    DocumentRecord,
    IndexingConflictError,
    IndexingJobRecord,
)
from ..interfaces.chunker import Chunk
from ..interfaces.parser import ParsedPage
from ..interfaces.retriever import RetrievedChunk


# --------------------------------------------------------------------------- #
# LLMClient
# --------------------------------------------------------------------------- #


class InMemoryLLMClient:
    """테스트용 결정론적 LLM. 호출 인자를 기록해 assert 가능."""

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        stream_chunks: list[str] | None = None,
        healthy: bool = True,
    ) -> None:
        self._responses = list(responses) if responses else ["mock answer"]
        self._stream_chunks = list(stream_chunks) if stream_chunks else ["mock ", "answer"]
        self._healthy = healthy
        self.calls: list[dict] = []  # generate / stream 호출 기록

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        guided_json_schema: dict | None = None,
        lora_adapter: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "kind": "generate",
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "guided_json_schema": guided_json_schema,
                "lora_adapter": lora_adapter,
            }
        )
        # FIFO consume; 마지막 응답을 sticky하게 사용
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    async def stream(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        lora_adapter: str | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "kind": "stream",
                "prompt": prompt,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "lora_adapter": lora_adapter,
            }
        )
        for c in self._stream_chunks:
            yield c

    async def health(self) -> bool:
        return self._healthy


# --------------------------------------------------------------------------- #
# Embedder
# --------------------------------------------------------------------------- #


class InMemoryEmbedder:
    """결정론적 hash-based 임베더 — 같은 텍스트 → 같은 벡터."""

    def __init__(self, *, model_name: str = "mock-embed", dense_dim: int = 8) -> None:
        self._model_name = model_name
        self._dense_dim = dense_dim

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dense_dim(self) -> int:
        return self._dense_dim

    def _dense(self, text: str) -> list[float]:
        # 결정론·bag-of-chars hashing — 같은 char은 같은 bucket, 다른 char은 다른 bucket으로
        # 흩어져 cosine 유사도가 텍스트 유사도와 단조 관계를 갖는다 (테스트에서
        # 동일 텍스트 ≈ 1.0, 무관 텍스트 ≈ 0~0.3).
        v = [0.0] * self._dense_dim
        for ch in text:
            bucket = (ord(ch) * 2654435761) % self._dense_dim  # Knuth multiplicative
            v[bucket] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def _sparse(self, text: str) -> dict[int, float]:
        # 토큰 byte → token_id 흉내. 운영 의미 없음, 결정론만 보장.
        out: dict[int, float] = {}
        for tok in text.split():
            tid = (hash(tok) & 0xFFFF) % 30522  # bert vocab 크기 흉내
            out[tid] = out.get(tid, 0.0) + 1.0
        return out

    async def embed_batch(
        self, texts: list[str]
    ) -> list[tuple[list[float], dict[int, float]]]:
        return [(self._dense(t), self._sparse(t)) for t in texts]

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        return self._dense(text), self._sparse(text)


# --------------------------------------------------------------------------- #
# Reranker
# --------------------------------------------------------------------------- #


class InMemoryReranker:
    """질문·후보 content의 token Jaccard로 결정론적 점수 부여."""

    def __init__(self, *, model_name: str = "mock-rerank") -> None:
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @staticmethod
    def _score(question: str, content: str) -> float:
        q = set(question.split())
        c = set(content.split())
        if not q or not c:
            return 0.0
        inter = len(q & c)
        union = len(q | c)
        return inter / union if union else 0.0

    async def rerank(
        self,
        question: str,
        candidates: list[RetrievedChunk],
        top_k: int = 10,
    ) -> list[RetrievedChunk]:
        for c in candidates:
            c.rerank_score = self._score(question, c.content)
        return sorted(
            candidates,
            key=lambda c: c.rerank_score or 0.0,
            reverse=True,
        )[:top_k]


# --------------------------------------------------------------------------- #
# VectorStore
# --------------------------------------------------------------------------- #


@dataclass
class _Point:
    id: str
    dense: list[float]
    sparse: dict[int, float]
    payload: dict[str, Any]


@dataclass
class _Collection:
    dense_dim: int
    with_sparse: bool
    points: dict[str, _Point] = field(default_factory=dict)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _sparse_dot(a: dict[int, float], b: dict[int, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def _condition_match(payload: dict[str, Any], cond: dict) -> bool:
    """단일 Qdrant condition dict 매칭 — must / should / is_null / range / match."""
    if "must" in cond:
        return all(_condition_match(payload, c) for c in cond["must"])
    if "should" in cond:
        return any(_condition_match(payload, c) for c in cond["should"])
    if "is_null" in cond:
        key = cond["is_null"].get("key")
        return payload.get(key) is None if key else True

    key = cond.get("key")
    if key is None:
        return True
    actual = payload.get(key)

    if "match" in cond:
        match = cond["match"]
        if "value" in match:
            return actual == match["value"]
        if "any" in match:
            allowed = match["any"]
            if isinstance(actual, list):
                return any(a in allowed for a in actual)
            return actual in allowed
        return True

    if "range" in cond:
        rng = cond["range"]
        if actual is None:
            return False
        if "lte" in rng and actual > rng["lte"]:
            return False
        if "gte" in rng and actual < rng["gte"]:
            return False
        if "lt" in rng and actual >= rng["lt"]:
            return False
        if "gt" in rng and actual <= rng["gt"]:
            return False
        return True

    return True


def _payload_match(payload: dict[str, Any], acl_filter: dict | None) -> bool:
    """ACL filter dict 전체 매칭 — top-level must의 모든 조건 충족 + must_not 모두 미충족.

    Qdrant filter 의미와 정합:
      - `must`: 모든 절이 참이어야 통과
      - `must_not`: 어느 절이라도 참이면 제외 (예: archived=true)
      - `should`: 본 함수에서는 미지원 (caller가 should를 must로 변환해 전달)
    """
    if not acl_filter:
        return True
    must = acl_filter.get("must") or []
    if not all(_condition_match(payload, c) for c in must):
        return False
    must_not = acl_filter.get("must_not") or []
    if any(_condition_match(payload, c) for c in must_not):
        return False
    return True


class InMemoryVectorStore:
    """순수 파이썬 dict 기반 VectorStore. DBSF 대용으로 dense+sparse 합산 정렬."""

    def __init__(self) -> None:
        self._collections: dict[str, _Collection] = {}

    def _coll(self, tenant_id: str) -> _Collection:
        c = self._collections.get(tenant_id)
        if c is None:
            raise KeyError(f"collection chunks_{tenant_id} does not exist")
        return c

    async def create_collection(
        self, *, tenant_id: str, dense_dim: int, with_sparse: bool = True
    ) -> None:
        if tenant_id in self._collections:
            raise ValueError(f"collection chunks_{tenant_id} already exists")
        self._collections[tenant_id] = _Collection(
            dense_dim=dense_dim, with_sparse=with_sparse
        )

    async def upsert_chunks(
        self, *, tenant_id: str, points: list[dict[str, Any]]
    ) -> None:
        coll = self._coll(tenant_id)
        for p in points:
            pid = str(p["id"])
            coll.points[pid] = _Point(
                id=pid,
                dense=list(p["dense_vector"]),
                sparse=dict(p.get("sparse_vector") or {}),
                payload=dict(p.get("payload") or {}),
            )

    async def hybrid_query(
        self,
        *,
        tenant_id: str,
        dense_query: list[float],
        sparse_query: dict[int, float],
        acl_filter: dict,
        top_k: int = 50,
    ) -> list[dict[str, Any]]:
        coll = self._coll(tenant_id)
        scored: list[tuple[float, _Point]] = []
        for p in coll.points.values():
            if not _payload_match(p.payload, acl_filter):
                continue
            d = _cosine(dense_query, p.dense)
            s = _sparse_dot(sparse_query, p.sparse)
            # DBSF는 분포 기반 정규화이지만, 테스트 mock에선 단순 평균으로 충분.
            scored.append(((d + s) / 2.0, p))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {"id": p.id, "score": score, "payload": dict(p.payload)}
            for score, p in scored[:top_k]
        ]

    async def set_payload(
        self, *, tenant_id: str, chunk_ids: list[str], payload: dict
    ) -> None:
        coll = self._coll(tenant_id)
        for cid in chunk_ids:
            p = coll.points.get(str(cid))
            if p is not None:
                p.payload.update(payload)

    async def delete_collection(self, tenant_id: str) -> None:
        self._collections.pop(tenant_id, None)

    async def delete_points(
        self, *, tenant_id: str, chunk_ids: list[str]
    ) -> None:
        coll = self._collections.get(tenant_id)
        if coll is None:
            return
        for cid in chunk_ids:
            coll.points.pop(str(cid), None)


# --------------------------------------------------------------------------- #
# DocumentParser / Chunker (테스트용 단순 구현)
# --------------------------------------------------------------------------- #


class InMemoryParser:
    """파일 경로 또는 inline 텍스트를 ParsedPage 리스트로 변환.

    `inline_pages` map에 path → list[str] (페이지별 텍스트)를 미리 등록하면 파일 I/O 없이 사용.
    그 외 경로는 디스크에서 텍스트로 읽어 단일 페이지로 처리.
    """

    parser_version: str = "p1"

    def __init__(
        self,
        *,
        parser_version: str = "p1",
        inline_pages: dict[str, list[str]] | None = None,
    ) -> None:
        self.parser_version = parser_version
        self._inline = dict(inline_pages or {})

    @classmethod
    def supported_extensions(cls) -> list[str]:
        return [".txt", ".md"]

    async def parse(self, file_path: str) -> list[ParsedPage]:
        pages = self._inline.get(file_path)
        if pages is None:
            text = Path(file_path).read_text(encoding="utf-8")
            pages = [text]
        out: list[ParsedPage] = []
        for i, body in enumerate(pages, start=1):
            out.append(
                ParsedPage(
                    page_number=i,
                    text=body,
                    section_title=None,
                    heading_path=[],
                )
            )
        return out


class InMemoryDocumentRepository:
    """documents 테이블 in-memory mock — (tenant_id, doc_id, version) → DocumentRecord."""

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str, str], DocumentRecord] = {}

    async def upsert(self, doc: DocumentRecord) -> None:
        self._docs[(doc.tenant_id, doc.doc_id, doc.version)] = doc

    async def get(
        self, *, tenant_id: str, doc_id: str, version: str
    ) -> DocumentRecord | None:
        return self._docs.get((tenant_id, doc_id, version))

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        keyword: str | None = None,
        approval_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[DocumentRecord]:
        # 같은 doc_id의 가장 최신 version만 노출
        latest: dict[str, DocumentRecord] = {}
        for (tid, did, _ver), rec in self._docs.items():
            if tid != tenant_id:
                continue
            existing = latest.get(did)
            if existing is None or _version_key(rec.version) > _version_key(existing.version):
                latest[did] = rec
        rows = list(latest.values())
        if keyword:
            kw = keyword.lower()
            rows = [
                r for r in rows
                if kw in (r.title or "").lower() or kw in (r.doc_id or "").lower()
            ]
        if approval_status:
            rows = [r for r in rows if r.approval_status == approval_status]
        rows.sort(key=lambda r: r.doc_id)
        return rows[offset : offset + limit]

    async def update_approval(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str,
        approval_status: str,
    ) -> DocumentRecord | None:
        rec = self._docs.get((tenant_id, doc_id, version))
        if rec is None:
            return None
        rec.approval_status = approval_status
        return rec

    async def delete(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str | None = None,
    ) -> int:
        keys = [
            k for k in self._docs.keys()
            if k[0] == tenant_id and k[1] == doc_id
            and (version is None or k[2] == version)
        ]
        for k in keys:
            self._docs.pop(k, None)
        return len(keys)

    async def update_partial(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        version: str,
        patch: dict,
    ) -> DocumentRecord | None:
        rec = self._docs.get((tenant_id, doc_id, version))
        if rec is None:
            return None
        for k, v in patch.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        return rec


def _version_key(v: str) -> tuple:
    """list_by_tenant에서 최신 version 비교 — 'v1','v2',...'v10' 같은 단순 패턴 지원."""
    import re

    m = re.match(r"v(\d+)$", v or "v1")
    return (int(m.group(1)),) if m else (0, v or "")


class InMemoryChunkRepository:
    """chunks 테이블 in-memory mock."""

    def __init__(self) -> None:
        # key: (tenant_id, doc_id, doc_version, parser_version) → list[ChunkRecord]
        self._chunks: dict[tuple[str, str, str, str], list[ChunkRecord]] = {}
        # chunk_id → ChunkRecord (metadata 갱신용)
        self._by_id: dict[tuple[str, str], ChunkRecord] = {}

    async def replace_chunks(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        doc_version: str,
        parser_version: str,
        chunks: list[ChunkRecord],
    ) -> None:
        key = (tenant_id, doc_id, doc_version, parser_version)
        # 기존 cleanup
        existing = self._chunks.pop(key, [])
        for c in existing:
            self._by_id.pop((tenant_id, c.chunk_id), None)
        # 신규 INSERT
        self._chunks[key] = list(chunks)
        for c in chunks:
            self._by_id[(tenant_id, c.chunk_id)] = c

    async def update_metadata(
        self,
        *,
        tenant_id: str,
        chunk_ids: list[str],
        metadata: dict[str, Any],
    ) -> None:
        for cid in chunk_ids:
            row = self._by_id.get((tenant_id, cid))
            if row is None:
                continue
            for k, v in metadata.items():
                if hasattr(row, k):
                    setattr(row, k, v)

    async def list_by_doc(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        doc_version: str,
        parser_version: str | None = None,
    ) -> list[ChunkRecord]:
        out: list[ChunkRecord] = []
        for (tid, did, ver, pv), chunks in self._chunks.items():
            if tid != tenant_id or did != doc_id or ver != doc_version:
                continue
            if parser_version and pv != parser_version:
                continue
            out.extend(chunks)
        return out

    async def delete_by_doc(
        self,
        *,
        tenant_id: str,
        doc_id: str,
        doc_version: str | None = None,
    ) -> list[str]:
        deleted_ids: list[str] = []
        keys_to_remove = []
        for (tid, did, ver, pv), chunks in self._chunks.items():
            if tid != tenant_id or did != doc_id:
                continue
            if doc_version is not None and ver != doc_version:
                continue
            for c in chunks:
                deleted_ids.append(c.chunk_id)
                self._by_id.pop((tenant_id, c.chunk_id), None)
            keys_to_remove.append((tid, did, ver, pv))
        for k in keys_to_remove:
            self._chunks.pop(k, None)
        return deleted_ids


class InMemoryIndexingJobRepository:
    """indexing_jobs in-memory mock — ADR-012 §10 unique constraint 흉내."""

    _ACTIVE_STATES = {"pending", "parsing", "chunking", "embedding", "indexing"}

    def __init__(self) -> None:
        self._jobs: dict[str, IndexingJobRecord] = {}

    async def create(self, job: IndexingJobRecord) -> None:
        for existing in self._jobs.values():
            if (
                existing.tenant_id == job.tenant_id
                and existing.doc_id == job.doc_id
                and existing.doc_version == job.doc_version
                and existing.status in self._ACTIVE_STATES
            ):
                raise IndexingConflictError(existing.job_id)
        self._jobs[job.job_id] = job

    async def update(
        self,
        *,
        job_id: str,
        status: str | None = None,
        step: str | None = None,
        progress: int | None = None,
        total_chunks: int | None = None,
        indexed_chunks: int | None = None,
        failed_chunks: list[dict[str, Any]] | None = None,
        error_message: str | None = None,
        failure_rate: float | None = None,
    ) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        if status is not None:
            job.status = status
        if step is not None:
            job.step = step
        if progress is not None:
            job.progress = progress
        if total_chunks is not None:
            job.total_chunks = total_chunks
        if indexed_chunks is not None:
            job.indexed_chunks = indexed_chunks
        if failed_chunks is not None:
            job.failed_chunks = list(failed_chunks)
        if error_message is not None:
            job.error_message = error_message
        if failure_rate is not None:
            job.failure_rate = failure_rate

    async def get(self, job_id: str) -> IndexingJobRecord | None:
        return self._jobs.get(job_id)

    async def list_by_tenant(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IndexingJobRecord]:
        out = [j for j in self._jobs.values() if j.tenant_id == tenant_id]
        if status is not None:
            out = [j for j in out if j.status == status]
        # 등록 순(=dict 삽입 순) 역순 — 운영 ADR-017 §7 created_at DESC와 정합.
        out.reverse()
        return out[offset : offset + limit]


class InMemoryChunker:
    """fixed-window chunker — 공백 기준 토큰 N개씩 분할 (overlap 옵션)."""

    chunk_strategy: str = "fixed-window-v1"

    def __init__(
        self,
        *,
        chunk_strategy: str = "fixed-window-v1",
        max_tokens: int = 100,
        overlap_tokens: int = 0,
    ) -> None:
        self.chunk_strategy = chunk_strategy
        self.max_tokens = max(1, max_tokens)
        self.overlap_tokens = max(0, min(overlap_tokens, max_tokens - 1))

    async def chunk(
        self,
        *,
        doc_id: str,
        doc_version: str,
        parser_version: str,
        pages: list[ParsedPage],
    ) -> list[Chunk]:
        chunks: list[Chunk] = []
        chunk_index = 0
        for page in pages:
            tokens = page.text.split()
            if not tokens:
                continue
            step = max(1, self.max_tokens - self.overlap_tokens)
            for start in range(0, len(tokens), step):
                window = tokens[start : start + self.max_tokens]
                if not window:
                    break
                content = " ".join(window)
                start_char = sum(len(t) + 1 for t in tokens[:start])
                end_char = start_char + len(content)
                chunks.append(
                    Chunk(
                        chunk_id=f"{doc_id}:{doc_version}:{parser_version}:chunk:{chunk_index}",
                        chunk_index=chunk_index,
                        content=content,
                        page_number=page.page_number,
                        section_title=page.section_title,
                        heading_path=list(page.heading_path or []),
                        start_char=start_char,
                        end_char=end_char,
                        token_count=len(window),
                    )
                )
                chunk_index += 1
                if start + self.max_tokens >= len(tokens):
                    break
        return chunks
