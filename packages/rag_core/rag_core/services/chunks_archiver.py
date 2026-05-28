"""ChunksArchiver — ADR-012 §3 chunks archival.

Protocol + InMemory 구현. 운영 Postgres 구현은 backend가 담당.

흐름:
  1. find_candidates(domain_id, threshold_date) → 후보 chunk ids
  2. archive(domain_id, chunk_ids, reason) →
       a. chunks_archive에 row insert
       b. chunks.archived_at = NOW() (row 보존, search에서는 active filter로 제외)
       c. caller가 별도로 vector_store.set_payload({"archived": True}) 호출
       d. 결과 ArchivalBatchResult 반환

본 service는 vector_store와 직접 결합하지 않는다 — caller(ArchivalWorker)가 set_payload를
호출해 layering을 분리. unit 테스트도 vector_store mock을 직접 검증할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Protocol

from rag_core.interfaces.chunk_repository import ChunkRecord


@dataclass
class ArchivalBatchResult:
    domain_id: str
    archived_chunk_ids: list[str] = field(default_factory=list)
    skipped_chunk_ids: list[str] = field(default_factory=list)  # 이미 archived
    reason: str = "valid_until_threshold"


class ChunksArchiver(Protocol):
    async def find_candidates(
        self,
        *,
        domain_id: str,
        valid_until_before: date,
    ) -> list[ChunkRecord]:
        """`valid_until < valid_until_before AND archived_at IS NULL` 검색."""
        ...

    async def archive(
        self,
        *,
        domain_id: str,
        chunks: list[ChunkRecord],
        reason: str = "valid_until_threshold",
    ) -> ArchivalBatchResult:
        """chunks_archive 적재 + chunks.archived_at set. 이미 archived인 row는 skip."""
        ...


# --------------------------------------------------------------------------- #
# InMemory — tests + dev backend
# --------------------------------------------------------------------------- #


class InMemoryChunksArchiver:
    """기존 InMemoryChunkRepository를 backing store로 사용. archive 호출 시 archive_records
    리스트에 사본을 누적하고 source records의 archived_at를 set한다.
    """

    def __init__(self, *, chunk_repo) -> None:
        self._repo = chunk_repo
        self.archive_records: list[dict] = []  # 디버그/테스트 가시성

    async def find_candidates(
        self,
        *,
        domain_id: str,
        valid_until_before: date,
    ) -> list[ChunkRecord]:
        out: list[ChunkRecord] = []
        # InMemoryChunkRepository._chunks: {(tid, doc, ver, parser): [ChunkRecord]}
        chunks_map = getattr(self._repo, "_chunks", {})
        for key, chunks in chunks_map.items():
            if key[0] != domain_id:
                continue
            for r in chunks:
                if r.valid_until is None:
                    continue
                # valid_until은 ChunkRecord에서 ISO 문자열 또는 date
                vu = r.valid_until if isinstance(r.valid_until, date) else date.fromisoformat(r.valid_until)
                already_archived = getattr(r, "archived_at", None) is not None
                if vu < valid_until_before and not already_archived:
                    out.append(r)
        return out

    async def archive(
        self,
        *,
        domain_id: str,
        chunks: list[ChunkRecord],
        reason: str = "valid_until_threshold",
    ) -> ArchivalBatchResult:
        now = datetime.now(timezone.utc)
        archived: list[str] = []
        skipped: list[str] = []
        for c in chunks:
            if getattr(c, "archived_at", None) is not None:
                skipped.append(c.chunk_id)
                continue
            self.archive_records.append(
                {
                    "domain_id": c.domain_id,
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "doc_version": c.doc_version,
                    "content": c.content,
                    "valid_until": c.valid_until,
                    "archived_at": now,
                    "archive_reason": reason,
                }
            )
            # 원본 ChunkRecord에 archived_at attribute 부여 — dataclass에 없어도 attr로 추가.
            c.archived_at = now  # type: ignore[attr-defined]
            archived.append(c.chunk_id)
        return ArchivalBatchResult(
            domain_id=domain_id,
            archived_chunk_ids=archived,
            skipped_chunk_ids=skipped,
            reason=reason,
        )
