# Nginx Setup — rag.aines.kr

DomainRAG Ops를 115번 운영 서버에 띄울 때의 nginx 리버스 프록시 절차.

AuthFusion(`console.aines.kr` / `api.aines.kr`)이 이미 운영 중이므로 동일 nginx 인스턴스에 conf.d만 추가한다.

## 사전 요건

- DNS A 레코드: `rag.aines.kr → 182.208.133.142`
- 와일드카드 인증서 `*.aines.kr` 이미 설치됨:
  - `/home/ju/ssl/aines.kr.fullchain.sectigo.pem`
  - `/home/ju/ssl/aines.kr.key.pem`
- 115번 서버에 `nginx` 시스템 패키지 설치 (AuthFusion이 이미 사용)
- AuthFusion sso-server에 OIDC client 등록 (per-tenant client_id, ADR-018 §3):
  - `redirect_uris`: `https://rag.aines.kr/auth/callback`
  - `post_logout_redirect_uris`: `https://rag.aines.kr/`

## 적용

```bash
ssh ju@192.168.0.115
cd ~/domainrag-ops
sudo cp config/nginx/rag.aines.kr.conf /etc/nginx/conf.d/rag.conf
sudo nginx -t
sudo systemctl reload nginx
```

이후 `config/nginx/rag.aines.kr.conf`를 수정하면 다시 cp + reload.

## 라우팅 요약

| Path | Backend | 비고 |
|---|---|---|
| `/api/{tenant_id}/chat/stream` | 127.0.0.1:8001 | SSE — `proxy_buffering off` |
| `/api/*` | 127.0.0.1:8001 | FastAPI 일반 |
| `/api/health` `/api/openapi.json` `/api/docs` `/api/redoc` | 127.0.0.1:8001 | 운영 진단 |
| `/` (그 외 모든 path) | 127.0.0.1:3000 | Next.js — `/auth/callback`, `/{tenant_id}/chat` 등 |

## 검증

```bash
curl -I https://rag.aines.kr/                              # 200 (Next.js)
curl -I https://rag.aines.kr/api/health                    # 200 (FastAPI)
curl -I https://rag.aines.kr/api/platform/admin/endpoints  # 401 (인증 필요 — 정상)
```

SSE 검증(인증 토큰 발급 후):

```bash
curl -N -X POST https://rag.aines.kr/api/security/chat/stream \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"question":"테스트","conversation_id":null}'
# event: token / data: {"text":"..."} 가 실시간 흘러나오면 OK
```

## 운영 주의

- AuthFusion `api.aines.kr`와 인증서·nginx 인스턴스를 공유한다. `nginx -t` 한 번이라도 실패하면 reload하지 말 것 (전체 plane 영향).
- `client_max_body_size 200m`는 PDF 문서 업로드 가정. 더 큰 파일을 받으려면 backend `IndexingOrchestrator`도 동시 검토.
- SSE timeout `proxy_read_timeout 1h` — chat_streaming은 보통 수십 초지만 retrieval 지연 + judge 발동 시 길어질 수 있음.
- HTTP → HTTPS 강제 redirect 적용됨. HSTS preload 등록 권장.
