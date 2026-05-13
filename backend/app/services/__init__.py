"""Backend application services — RAG·Indexing·Auth 등의 orchestrator.

각 service는 rag_core의 Protocol 어댑터·service class를 DI로 받아 결선한다.
FastAPI dependency는 app.deps에서 노출.
"""
