# 문서 목록

DomainRAG Ops 프로젝트 문서 및 가이드

---

## 📋 필수 문서

| 문서 | 내용 |
|-----|-----|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 시스템 아키텍처 및 컴포넌트 설명 |
| [API.md](./API.md) | REST API 완전 명세서 |
| [SETUP.md](./SETUP.md) | 개발 환경 설치 가이드 |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | 프로덕션 배포 가이드 |
| [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) | 문제 해결 가이드 |

---

## 📐 Architecture Decision Records (ADR)

프로젝트 설계 결정을 기록합니다.

| ADR | 제목 | 상태 |
|-----|-----|------|
| [ADR-001](./adr/001-citation-metadata-design.md) | Citation 메타데이터 설계 | Accepted |
| [ADR-002](./adr/002-protocol-adapter-pattern.md) | Protocol/Adapter 패턴 | Accepted |
| [ADR-003](./adr/003-langraph-orchestration.md) | LangGraph 오케스트레이션 | Accepted |
| [ADR-004](./adr/004-security-acl-model.md) | 보안 & ACL 모델 | Accepted |

---

## 📖 프로젝트별 README

각 서브프로젝트의 상세 문서:

- [frontend/README.md](../frontend/README.md) - Next.js 프론트엔드
- [backend/README.md](../backend/README.md) - FastAPI 백엔드
- [packages/rag_core/README.md](../packages/rag_core/README.md) - RAG 핵심 라이브러리

---

## 🚀 빠른 시작

1. **개발 환경 설정** → [SETUP.md](./SETUP.md)
2. **아키텍처 이해** → [ARCHITECTURE.md](./ARCHITECTURE.md)
3. **API 명세 확인** → [API.md](./API.md)
4. **배포 준비** → [DEPLOYMENT.md](./DEPLOYMENT.md)

---

## 🔧 설정 파일

설정 관련 가이드:

- `configs/models.yaml` - 모델 설정
- `configs/retrieval.yaml` - 검색 설정
- `configs/security.yaml` - 보안 설정
- `configs/prompts.yaml` - 프롬프트 템플릿
- `configs/evaluation.yaml` - 평가 설정

---

## 📚 학습 자료

- [IMPLEMENTATION_SPEC.md](../IMPLEMENTATION_SPEC.md) - 전체 구현 명세
- [CLAUDE.md](../CLAUDE.md) - Claude Code 개발 가이드

---

## ❓ FAQ

자주 묻는 질문:

- Citation이 어떻게 생성되나? → [ARCHITECTURE.md](./ARCHITECTURE.md#citation-flow)
- 어떻게 새로운 모델을 추가하나? → [README.md](../packages/rag_core/README.md#확장-가능성)
- 문서 권한이 어떻게 작동하나? → [ADR-004](./adr/004-security-acl-model.md)

---

## 🤝 기여 가이드

코드를 기여할 때:

1. [ARCHITECTURE.md](./ARCHITECTURE.md)의 설계 원칙 확인
2. [CLAUDE.md](../CLAUDE.md)의 절대 원칙 준수
3. 각 서브프로젝트의 README 참고
4. 테스트 작성 및 타입힌트 포함

---

## 📞 지원

문제가 있으면:

1. [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) 확인
2. 관련 ADR 검토
3. 해당 서브프로젝트의 README 참고

---

**마지막 업데이트**: 2026-05-08
