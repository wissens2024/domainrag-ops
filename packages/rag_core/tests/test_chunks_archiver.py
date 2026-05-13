"""InMemoryChunksArchiver — ADR-012 §3 후보 탐색 + archive 동작 단위 검증."""

from __future__ import annotations

from datetime import date

from rag_core.clients.in_memory import InMemoryChunkRepository
from rag_core.interfaces.chunk_repository import ChunkRecord
from rag_core.services.chunks_archiver import InMemoryChunksArchiver


def _chunk(
    *,
    tenant_id: str,
    chunk_id: str,
    doc_id: str = "d1",
    valid_until: str | None = None,
) -> ChunkRecord:
    return ChunkRecord(
        tenant_id=tenant_id,
        chunk_id=chunk_id,
        doc_id=doc_id,
        doc_version="v1",
        parser_version="p1",
        chunk_strategy="fixed-window-v1",
        chunk_index=0,
        content=f"content {chunk_id}",
        title="test",
        page_number=1,
        section_title=None,
        heading_path=[],
        content_hash=None,
        start_char=0,
        end_char=10,
        token_count=2,
        department=None,
        doc_type=None,
        security_level="internal",
        acl=[],
        approval_status="approved",
        tags=[],
        valid_from=None,
        valid_until=valid_until,
    )


async def _populate(repo: InMemoryChunkRepository, chunks: list[ChunkRecord]) -> None:
    by_key: dict[tuple, list[ChunkRecord]] = {}
    for c in chunks:
        key = (c.tenant_id, c.doc_id, c.doc_version, c.parser_version)
        by_key.setdefault(key, []).append(c)
    for (tid, did, ver, parser), batch in by_key.items():
        await repo.replace_chunks(
            tenant_id=tid, doc_id=did, doc_version=ver,
            parser_version=parser, chunks=batch,
        )


async def test_find_candidates_filters_by_valid_until_and_tenant():
    repo = InMemoryChunkRepository()
    await _populate(
        repo,
        [
            _chunk(tenant_id="t1", chunk_id="c-old-1", valid_until="2026-01-01"),
            _chunk(tenant_id="t1", chunk_id="c-old-2", valid_until="2026-01-15"),
            _chunk(tenant_id="t1", chunk_id="c-future", valid_until="2026-12-01"),
            _chunk(tenant_id="t1", chunk_id="c-no-vu"),  # valid_until None → 제외
            _chunk(tenant_id="t2", chunk_id="c-other", valid_until="2026-01-01"),
        ],
    )
    archiver = InMemoryChunksArchiver(chunk_repo=repo)
    cands = await archiver.find_candidates(
        tenant_id="t1", valid_until_before=date(2026, 5, 1)
    )
    ids = sorted(c.chunk_id for c in cands)
    assert ids == ["c-old-1", "c-old-2"]


async def test_archive_appends_records_and_marks_archived_at():
    repo = InMemoryChunkRepository()
    chunks = [
        _chunk(tenant_id="t1", chunk_id="c-old-1", valid_until="2026-01-01"),
        _chunk(tenant_id="t1", chunk_id="c-old-2", valid_until="2026-01-15"),
    ]
    await _populate(repo, chunks)
    archiver = InMemoryChunksArchiver(chunk_repo=repo)
    cands = await archiver.find_candidates(
        tenant_id="t1", valid_until_before=date(2026, 5, 1)
    )

    result = await archiver.archive(tenant_id="t1", chunks=cands)
    assert sorted(result.archived_chunk_ids) == ["c-old-1", "c-old-2"]
    assert result.skipped_chunk_ids == []
    assert len(archiver.archive_records) == 2
    # archived_at attribute가 부여됨
    for c in cands:
        assert getattr(c, "archived_at", None) is not None

    # 다시 find하면 이미 archived → 후보 0
    again = await archiver.find_candidates(
        tenant_id="t1", valid_until_before=date(2026, 5, 1)
    )
    assert again == []


async def test_archive_skips_already_archived_chunks():
    repo = InMemoryChunkRepository()
    c = _chunk(tenant_id="t1", chunk_id="c-1", valid_until="2026-01-01")
    c.archived_at = "preset"  # type: ignore[attr-defined]
    await _populate(repo, [c])
    archiver = InMemoryChunksArchiver(chunk_repo=repo)
    result = await archiver.archive(tenant_id="t1", chunks=[c])
    assert result.archived_chunk_ids == []
    assert result.skipped_chunk_ids == ["c-1"]
    assert archiver.archive_records == []
