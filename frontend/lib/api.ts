/**
 * DomainRAG Ops — API client (ADR-017).
 *
 * 모든 endpoint를 함수 단위로 노출. SWR의 fetcher로 사용 가능.
 */

import type {
  AdapterRecord,
  AssessmentAnalytics,
  AssessmentExtractResult,
  AssessmentImportResult,
  AssessmentItem,
  AssessmentListResult,
  AssessmentQualityStatus,
  ChatLogListResult,
  ChatLogRow,
  ChatResponse,
  CitationDistributionResult,
  CitationReverifyResult,
  ConfigChangeRow,
  Conversation,
  ConversationDetail,
  ConversationListResult,
  DashboardSnapshot,
  DomainMember,
  DocumentDetail,
  DocumentListResult,
  DryrunResult,
  EndpointHealthRow,
  EvalDataset,
  EvalJob,
  EvalJobListResult,
  IndexingJob,
  IndexingJobListResult,
  InputSchemaHistory,
  InputSchemaRecord,
  InputTypeSchemaJson,
  LoRAStatus,
  MyDomainsResult,
  PlatformUsageRow,
  PromptListResult,
  PromptPreviewResult,
  PromptRecord,
  ReindexMode,
  RoutingConfig,
  TenantListResult,
  TenantRow,
  TenantStatus,
  UiMode,
  UserContext,
} from './types';

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ||
  process.env.NEXT_PUBLIC_API_BASE_URL ||
  'http://localhost:8001';

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    const msg =
      typeof detail === 'object' && detail !== null && 'error' in detail
        ? String((detail as Record<string, unknown>).error)
        : `HTTP ${status}`;
    super(msg);
    this.status = status;
    this.detail = detail;
  }
}

// ADR-018 §6 — Token refresh interceptor (httpOnly cookie only).
// 401 응답 시 단일 mutex로 refresh 1회 시도 + 원 요청 재시도. refresh 실패는
// /api/auth/authorize/{domainId} redirect.
// XSS 노출면 최소화 — access_token을 localStorage에 두지 않는다. 모든 인증은
// httpOnly cookie(`domainrag_access`/`domainrag_refresh`)로만 이루어진다.
let _refreshInflight: Promise<boolean> | null = null;

async function _attemptRefresh(): Promise<boolean> {
  if (_refreshInflight) return _refreshInflight;
  _refreshInflight = (async () => {
    try {
      // domain_id는 URL path에서 결정. SSR safe-guard.
      const domainId =
        typeof window !== 'undefined'
          ? (window.location.pathname.split('/')[1] || '')
          : '';
      const res = await fetch(`${API_BASE}/api/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ domain_id: domainId }),
      });
      return res.ok;
    } catch {
      return false;
    } finally {
      // 다음 401에 다시 시도 가능하도록 inflight 해제 (성공·실패 동일)
      setTimeout(() => {
        _refreshInflight = null;
      }, 0);
    }
  })();
  return _refreshInflight;
}

// special path는 tenant가 아니라서 SSO authorize 시작에 첫 segment를 쓰면 안 됨.
// 이들은 페이지 자체가 미인증 처리 (랜딩 + 자동 SSO 또는 안내 UI).
const _NON_TENANT_SEGMENTS = new Set(['', 'platform', 'auth', 'account', 'console', '_next']);

function _redirectToLogin(): void {
  if (typeof window === 'undefined') return;
  const pathname = window.location.pathname;
  // root('/'), /auth/*, /account/*, /console 는 redirect 트리거 skip
  // (페이지 자체가 미인증 분기를 가짐 — 무한 루프 방지 + 잘못된 tenant authorize 방지).
  if (
    pathname === '/' ||
    pathname.startsWith('/auth/') ||
    pathname.startsWith('/account') ||
    pathname === '/console'
  ) {
    return;
  }
  const domainId = pathname.split('/')[1] || '';
  if (domainId && !_NON_TENANT_SEGMENTS.has(domainId)) {
    // `?redirect=1`이면 backend가 IdP authorize URL로 302를 이어 준다 (ADR-018 §2).
    window.location.href = `${API_BASE}/api/auth/authorize/${domainId}?redirect=1`;
  }
}

async function _fetchWithRefresh(
  url: string,
  init: RequestInit,
): Promise<Response> {
  const res = await fetch(url, init);
  if (res.status !== 401) return res;

  // 401 — token_expired 가정. refresh 시도 후 원 요청 재시도 1회만.
  // refresh 성공 시 새 httpOnly cookie가 set되어 있으므로 같은 init 그대로
  // 재요청. Authorization header는 사용 안 함.
  const refreshed = await _attemptRefresh();
  if (!refreshed) {
    _redirectToLogin();
    return res;
  }
  return fetch(url, init);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  };
  // 인증은 httpOnly cookie로만 — Authorization header는 service-to-service
  // 호출이 명시적으로 init.headers에 넣을 때만 사용.
  const res = await _fetchWithRefresh(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ error: 'request_failed' }));
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return res.json() as Promise<T>;
}

async function requestForm<T>(path: string, formData: FormData): Promise<T> {
  // 인증은 httpOnly cookie로만 (credentials: 'include'). Content-Type은 브라우저가
  // multipart boundary와 함께 자동 설정하므로 지정하지 않는다.
  const res = await _fetchWithRefresh(`${API_BASE}${path}`, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ error: 'request_failed' }));
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

// SWR fetcher (URL → JSON)
export const swrFetcher = <T = unknown>(path: string) => request<T>(path);

// ============================================================================
// Chat / Conversation / Feedback (ADR-017 §3·§4·§5)
// ============================================================================

export async function chat(
  domainId: string,
  body: {
    question: string;
    conversation_id?: string;
    ui_mode_request?: 'structured' | 'streaming';
  },
): Promise<ChatResponse> {
  return request<ChatResponse>(`/api/${domainId}/chat`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function postFeedback(
  domainId: string,
  body: { message_id: string; feedback: 'good' | 'bad'; comment?: string },
): Promise<void> {
  await request(`/api/${domainId}/feedback`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function listConversations(
  domainId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<ConversationListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request<ConversationListResult>(`/api/${domainId}/conversations?${qs}`);
}

export async function getConversation(
  domainId: string,
  conversationId: string,
): Promise<ConversationDetail> {
  return request(`/api/${domainId}/conversations/${conversationId}`);
}

export async function updateConversationTitle(
  domainId: string,
  conversationId: string,
  title: string,
): Promise<Conversation> {
  return request(`/api/${domainId}/conversations/${conversationId}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  });
}

export async function deleteConversation(
  domainId: string,
  conversationId: string,
): Promise<void> {
  await request(`/api/${domainId}/conversations/${conversationId}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// User (me)
// ============================================================================

export async function getCurrentUser(): Promise<UserContext> {
  // ADR-016 §3 Y9 — cross-tenant /api/auth/me 단일 진입점. tenant path mirror
  // 검증 없이 token만으로 roles 추출.
  return request<UserContext>(`/api/auth/me`);
}

// ADR-016 — 로그인 후 역할-aware 착지(단일 진실 소스). 콜백·/console·랜딩이 공유.
// 관리자를 채팅에 강제 경유시키지 않고 역할대로 바로 보낸다. 채팅은 관리 화면의
// 명시적 링크/도메인 칩으로 접근 유지.
export function postLoginDestination(user: UserContext): string {
  if (user.is_platform_admin) return '/platform/admin/tenants';
  if (user.is_admin || user.is_auditor) return `/${user.domain_id}/admin/dashboard`;
  return `/${user.domain_id}/chat`;
}

export async function getMyDomains(): Promise<MyDomainsResult> {
  // ADR-022 §7 — 내가 접근 가능한 도메인 목록 (도메인 switcher).
  return request<MyDomainsResult>(`/api/auth/me/domains`);
}

// ============================================================================
// AuthFusion Self-Service API (spec: docs/integration/authfusion-self-service-v1.md)
// backend proxy at /api/auth/account/*  → upstream /api/v1/me/*
// ============================================================================

export interface AccountSummary {
  sub: string;
  username: string;
  email: string;
  userSource: string;
}

// 실제 AuthFusion 운영 응답 shape (spec docs와 일부 field 다름 — 운영 ground truth).
export interface UserApplicationSummary {
  clientUuid: string;
  clientId: string;
  clientName: string;
  enabled: boolean;
  mfaRequired: boolean;
  roles: string[];
  grantedAt?: string;
}

export interface SessionInfo {
  sessionId: string;
  userId?: string;
  username?: string;
  ipAddress: string;
  userAgent: string;
  status?: string;
  createdAt: string;
  lastAccessedAt: string;
  expiresAt: string;
}

export interface MfaStatusResponse {
  userId?: string;
  totpEnabled: boolean;
  totpVerified: boolean;
  recoveryCodesRemaining: number;
  totpEnabledAt?: string;
}

export interface TotpSetupResponse {
  secret: string;
  qrCodeUri: string;
  recoveryCodes: string[];
}

async function accountFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api/auth/account${path}`, {
    credentials: 'include',
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(res.status, body);
  }
  if (res.status === 204) return undefined as unknown as T;
  const text = await res.text();
  return text ? (JSON.parse(text) as T) : (undefined as unknown as T);
}

export const account = {
  getSummary: () => accountFetch<AccountSummary>('/summary'),
  getApplications: () => accountFetch<UserApplicationSummary[]>('/applications'),
  getSessions: () => accountFetch<SessionInfo[]>('/sessions'),
  revokeSession: (sessionId: string) =>
    accountFetch<void>(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
  getMfaStatus: () => accountFetch<MfaStatusResponse>('/mfa/status'),
  setupMfa: () => accountFetch<TotpSetupResponse>('/mfa/setup', { method: 'POST' }),
  verifyMfaSetup: (code: string) =>
    accountFetch<void>('/mfa/verify-setup', {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
  disableMfa: () => accountFetch<void>('/mfa/disable', { method: 'POST' }),
  regenerateRecoveryCodes: () =>
    accountFetch<string[]>('/mfa/recovery-codes/regenerate', { method: 'POST' }),
  changePassword: (currentPassword: string, newPassword: string) =>
    accountFetch<void>('/change-password', {
      method: 'POST',
      body: JSON.stringify({ currentPassword, newPassword }),
    }),
};

/**
 * Logout — backend가 access/refresh 쿠키의 token을 revoke + 쿠키 삭제 (ADR-018 §6).
 * 호출자가 navigation은 직접. domain_id는 backend가 client_id resolve에 필요.
 */
// ADR-022 — SP 쿠키 삭제 + IdP end_session URL 반환. caller는 반환된 URL로 이동해야
// AuthFusion SSO 세션까지 종료된다(아니면 다음 진입 시 silent 재로그인). 없으면 null.
export async function logout(domainId: string): Promise<string | null> {
  try {
    const res = await fetch(`${API_BASE}/api/auth/logout`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain_id: domainId }),
    });
    if (!res.ok) return null;
    const body = (await res.json().catch(() => null)) as { logout_url?: string } | null;
    return body?.logout_url ?? null;
  } catch {
    return null;
  }
}

// ============================================================================
// Chat Streaming (ADR-013 §6, ADR-017 §3.2) — SSE
// ============================================================================

export interface StreamingChatHandlers {
  onToken: (text: string) => void;
  onComplete: (payload: { message_id: string; metadata: Record<string, unknown> }) => void;
  onFallback?: (payload: Record<string, unknown>) => void;
  onError?: (payload: Record<string, unknown>) => void;
}

/**
 * `/api/{tid}/chat/stream` SSE. fetch + ReadableStream으로 cookie/credentials
 * 정상 전송. EventSource는 credentials 미지원이라 사용 안 함.
 */
export async function chatStream(
  domainId: string,
  body: { question: string; conversation_id?: string },
  handlers: StreamingChatHandlers,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/${domainId}/chat/stream`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, ui_mode_request: 'streaming' }),
  });
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => ({ error: 'stream_failed' }));
    throw new ApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 메시지는 빈 줄("\n\n")으로 구분
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = 'message';
      const dataLines: string[] = [];
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      const data = dataLines.join('\n');
      if (!data) continue;
      try {
        const parsed = JSON.parse(data);
        if (event === 'token') handlers.onToken(String(parsed.text ?? ''));
        else if (event === 'complete') handlers.onComplete(parsed);
        else if (event === 'fallback') handlers.onFallback?.(parsed);
        else if (event === 'error') handlers.onError?.(parsed);
      } catch {
        // ignore malformed SSE chunk
      }
    }
  }
}

export async function eraseMyChatLogs(
  domainId: string,
  body: { mode: 'mask_only' | 'hard_delete'; reason: string },
): Promise<void> {
  await request(`/api/${domainId}/me/chat_logs`, {
    method: 'DELETE',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Dashboard (ADR-017 §10)
// ============================================================================

export async function getDashboard(domainId: string): Promise<DashboardSnapshot> {
  return request(`/api/${domainId}/admin/dashboard`);
}

// ============================================================================
// Admin — Documents (ADR-017 §6)
// ============================================================================

export async function listDocuments(
  domainId: string,
  params: {
    keyword?: string;
    approval_status?: string;
    page?: number;
    page_size?: number;
  } = {},
): Promise<DocumentListResult> {
  const qs = new URLSearchParams();
  if (params.keyword) qs.set('keyword', params.keyword);
  if (params.approval_status) qs.set('approval_status', params.approval_status);
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${domainId}/admin/documents?${qs}`);
}

export async function getDocument(
  domainId: string,
  docId: string,
  version = 'v1',
): Promise<DocumentDetail> {
  return request(`/api/${domainId}/admin/documents/${docId}?version=${version}`);
}

export async function uploadDocument(
  domainId: string,
  file: File,
  metadata: Record<string, unknown>,
  inputType?: string,
): Promise<{ job_id: string; doc_id: string; doc_version: string; status: string }> {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('metadata', JSON.stringify(metadata));
  if (inputType) fd.append('input_type', inputType);
  return requestForm(`/api/${domainId}/admin/documents/upload`, fd);
}

export async function reindexDocument(
  domainId: string,
  docId: string,
  mode: ReindexMode,
  version = 'v1',
): Promise<{ job_id: string; doc_id: string; status: string }> {
  return request(`/api/${domainId}/admin/documents/${docId}/reindex?version=${version}`, {
    method: 'POST',
    body: JSON.stringify({ mode }),
  });
}

export async function patchDocumentApproval(
  domainId: string,
  docId: string,
  body: { status: 'pending' | 'approved' | 'archived'; reason?: string; version?: string },
): Promise<{ doc_id: string; affected_chunks: number }> {
  return request(`/api/${domainId}/admin/documents/${docId}/approval`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function patchDocumentMetadata(
  domainId: string,
  docId: string,
  body: { patch: Record<string, unknown>; version?: string; reason?: string },
): Promise<{ doc_id: string; affected_chunks: number; synced_keys: string[] }> {
  return request(`/api/${domainId}/admin/documents/${docId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function hardDeleteDocument(
  domainId: string,
  docId: string,
  body: {
    reason: string;
    chat_logs_action?: 'keep_excerpts' | 'mask_excerpts' | 'delete_logs';
    version?: string;
  },
): Promise<{
  removed_chunks: number;
  removed_documents: number;
  storage_files: number;
  affected_chat_logs: number;
  dead_letters: unknown[];
}> {
  return request(`/api/${domainId}/admin/documents/${docId}/hard`, {
    method: 'DELETE',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Indexing (ADR-017 §7)
// ============================================================================

export async function listIndexingJobs(
  domainId: string,
  params: { page?: number; page_size?: number; status?: string } = {},
): Promise<IndexingJobListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  if (params.status) qs.set('status', params.status);
  return request(`/api/${domainId}/admin/indexing/jobs?${qs}`);
}

export async function getIndexingJob(
  domainId: string,
  jobId: string,
): Promise<IndexingJob> {
  return request(`/api/${domainId}/admin/indexing/jobs/${jobId}`);
}

export async function retryIndexingJob(
  domainId: string,
  jobId: string,
): Promise<{ job_id: string; doc_id: string; status: string }> {
  return request(`/api/${domainId}/admin/indexing/jobs/${jobId}/retry`, {
    method: 'POST',
  });
}

// ============================================================================
// Admin — Schema (ADR-017 §15)
// ============================================================================

export async function listInputSchemas(
  domainId: string,
): Promise<{
  domain_id: string;
  input_types: Array<{
    name: string;
    display_name: string;
    schema: InputTypeSchemaJson;
  }>;
}> {
  return request(`/api/${domainId}/admin/input_schemas`);
}

export async function getSchema(domainId: string): Promise<InputSchemaRecord> {
  return request(`/api/${domainId}/admin/schema`);
}

export async function putSchema(
  domainId: string,
  body: {
    schema_yaml: Record<string, unknown>;
    ui_schema_yaml?: Record<string, unknown>;
    base_version?: string;
  },
): Promise<{ record: InputSchemaRecord; deprecated_version: string | null }> {
  return request(`/api/${domainId}/admin/schema`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

export async function getSchemaHistory(
  domainId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<InputSchemaHistory> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${domainId}/admin/schema/history?${qs}`);
}

// ============================================================================
// Admin — Chat Logs (ADR-017 §8)
// ============================================================================

export interface ChatLogListFilters {
  user_id?: string;
  conversation_id?: string;
  from_date?: string;
  to_date?: string;
  fallback_only?: boolean;
  ui_mode?: UiMode;
  citation_type?: string;
  min_confidence?: number;
  max_confidence?: number;
  keyword?: string;
  page?: number;
  page_size?: number;
}

export async function listChatLogs(
  domainId: string,
  filters: ChatLogListFilters = {},
): Promise<ChatLogListResult> {
  const qs = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
  });
  return request(`/api/${domainId}/admin/logs/chat?${qs}`);
}

export async function getChatLog(
  domainId: string,
  requestId: string,
): Promise<ChatLogRow> {
  return request(`/api/${domainId}/admin/logs/chat/${requestId}`);
}

// ============================================================================
// Admin — Citation Inspector (ADR-017 §9)
// ============================================================================

export async function getCitationDistribution(
  domainId: string,
  params: { from_date?: string; to_date?: string; group_by?: 'day' | 'hour' } = {},
): Promise<CitationDistributionResult> {
  const qs = new URLSearchParams();
  if (params.from_date) qs.set('from_date', params.from_date);
  if (params.to_date) qs.set('to_date', params.to_date);
  if (params.group_by) qs.set('group_by', params.group_by);
  return request(`/api/${domainId}/admin/citation-inspector/distribution?${qs}`);
}

export async function getCitationSegments(
  domainId: string,
  messageId: string,
): Promise<{
  request_id: string;
  citations_by_type: Record<string, unknown[]>;
  verifier_metrics: Record<string, unknown>;
  inference_judge_results: unknown[];
  conflict_groups: unknown;
}> {
  return request(`/api/${domainId}/admin/citation-inspector/segments/${messageId}`);
}

export async function reverifyCitations(
  domainId: string,
  body: { from_date?: string; to_date?: string; max_records?: number },
): Promise<CitationReverifyResult> {
  return request(`/api/${domainId}/admin/citation-inspector/reverify`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Routing (ADR-017 §13)
// ============================================================================

export async function getRouting(domainId: string): Promise<RoutingConfig> {
  return request(`/api/${domainId}/admin/routing`);
}

export async function putRouting(
  domainId: string,
  value: RoutingConfig,
  reason?: string,
): Promise<{ domain_id: string; routing: RoutingConfig }> {
  return request(`/api/${domainId}/admin/routing`, {
    method: 'PUT',
    body: JSON.stringify({ value, reason }),
  });
}

export async function dryrunRouting(
  domainId: string,
  body: {
    classifier_decision: Record<string, unknown>;
    sample_query?: string;
    routing_config?: RoutingConfig;
    retrieval_confidence?: number;
  },
): Promise<DryrunResult> {
  return request(`/api/${domainId}/admin/routing/dryrun`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Prompts (ADR-017 §12)
// ============================================================================

export async function listPrompts(domainId: string): Promise<PromptListResult> {
  return request(`/api/${domainId}/admin/prompts`);
}

export async function getPrompt(
  domainId: string,
  task: string,
  version?: string,
  abSlot?: string,
): Promise<PromptRecord> {
  const qs = new URLSearchParams();
  if (version) qs.set('version', version);
  if (abSlot) qs.set('ab_slot', abSlot);
  return request(`/api/${domainId}/admin/prompts/${task}?${qs}`);
}

export async function patchPrompt(
  domainId: string,
  task: string,
  version: string,
  abSlot: string,
  body: { system?: string; user?: string; reason?: string },
): Promise<PromptRecord> {
  return request(`/api/${domainId}/admin/prompts/${task}/${version}/${abSlot}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function previewPrompt(
  domainId: string,
  task: string,
  body: {
    system?: string;
    user?: string;
    sample_question: string;
    sample_contexts?: Array<Record<string, unknown>>;
    invoke_llm?: boolean;
  },
): Promise<PromptPreviewResult> {
  return request(`/api/${domainId}/admin/prompts/${task}/preview`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — LoRA Registry (ADR-017 §14)
// ============================================================================

export async function listLoRA(
  domainId: string,
  status?: LoRAStatus,
): Promise<{ items: AdapterRecord[]; total: number }> {
  const qs = new URLSearchParams();
  if (status) qs.set('status', status);
  return request(`/api/${domainId}/admin/lora?${qs}`);
}

export async function uploadLoRA(
  domainId: string,
  file: File,
  metadata: {
    adapter_id: string;
    version?: string;
    base_model?: string;
    training_metadata?: Record<string, unknown>;
  },
): Promise<AdapterRecord> {
  const fd = new FormData();
  fd.append('weights', file);
  fd.append('metadata', JSON.stringify(metadata));
  return requestForm(`/api/${domainId}/admin/lora/upload`, fd);
}

export async function activateLoRA(domainId: string, adapterId: string): Promise<AdapterRecord> {
  return request(`/api/${domainId}/admin/lora/${adapterId}/activate`, { method: 'POST' });
}

export async function retireLoRA(domainId: string, adapterId: string): Promise<AdapterRecord> {
  return request(`/api/${domainId}/admin/lora/${adapterId}/retire`, { method: 'POST' });
}

export async function deleteLoRA(domainId: string, adapterId: string): Promise<void> {
  await request(`/api/${domainId}/admin/lora/${adapterId}`, { method: 'DELETE' });
}

// ============================================================================
// Admin — Evaluation (ADR-017 §16)
// ============================================================================

export async function listEvalDatasets(
  domainId: string,
): Promise<{ items: EvalDataset[] }> {
  return request(`/api/${domainId}/admin/evaluation/datasets`);
}

export async function runEvalJob(
  domainId: string,
  body: { dataset_name: string; config_override?: Record<string, unknown> },
): Promise<{ job_id: string; status: string }> {
  return request(`/api/${domainId}/admin/evaluation/run`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function listEvalJobs(
  domainId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<EvalJobListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${domainId}/admin/evaluation/jobs?${qs}`);
}

export async function getEvalJob(domainId: string, jobId: string): Promise<EvalJob> {
  return request(`/api/${domainId}/admin/evaluation/jobs/${jobId}`);
}

export async function promoteEvalJob(
  domainId: string,
  jobId: string,
  body: { target: string; version: string; reason?: string },
): Promise<EvalJob> {
  return request(`/api/${domainId}/admin/evaluation/jobs/${jobId}/promote`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Configs (ADR-017 §11)
// ============================================================================

export async function getConfigs(domainId: string): Promise<Record<string, unknown>> {
  return request(`/api/${domainId}/admin/configs`);
}

export async function getConfigCategory(
  domainId: string,
  category: string,
): Promise<Record<string, unknown>> {
  return request(`/api/${domainId}/admin/configs/${category}`);
}

export async function patchConfig(
  domainId: string,
  category: string,
  body: { key: string; value: unknown; reason?: string },
): Promise<{ category: string; key: string; old_value: unknown; new_value: unknown }> {
  return request(`/api/${domainId}/admin/configs/${category}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function reloadConfig(domainId: string): Promise<void> {
  await request(`/api/${domainId}/admin/configs/reload`, { method: 'POST' });
}

export async function getConfigHistory(
  domainId: string,
  params: { category: string; page?: number; page_size?: number },
): Promise<{ items: ConfigChangeRow[]; total: number; page: number; page_size: number }> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  const suffix = qs.toString() ? `?${qs}` : '';
  return request(
    `/api/${domainId}/admin/configs/${encodeURIComponent(params.category)}/history${suffix}`,
  );
}

// ============================================================================
// Admin — Assessment (ADR-017 §17)
// ============================================================================

export async function listAssessmentItems(
  domainId: string,
  params: {
    keyword?: string;
    subject?: string;
    difficulty?: string;
    quality_status?: AssessmentQualityStatus;
    page?: number;
    page_size?: number;
  } = {},
): Promise<AssessmentListResult> {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null) qs.set(k, String(v));
  });
  return request(`/api/${domainId}/admin/assessment/items?${qs}`);
}

export async function createAssessmentItem(
  domainId: string,
  body: Partial<AssessmentItem>,
): Promise<AssessmentItem> {
  return request(`/api/${domainId}/admin/assessment/items`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function patchAssessmentItem(
  domainId: string,
  itemId: string,
  body: Partial<AssessmentItem>,
): Promise<AssessmentItem> {
  return request(`/api/${domainId}/admin/assessment/items/${itemId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function approveAssessmentItem(
  domainId: string,
  itemId: string,
): Promise<AssessmentItem> {
  return request(`/api/${domainId}/admin/assessment/items/${itemId}/approve`, {
    method: 'POST',
  });
}

// draft/reviewed 매칭 item 일괄 승인 (ADR-014 §5). 필터 미지정이면 도메인 전체.
export async function bulkApproveAssessment(
  domainId: string,
  filters: { subject?: string; chapter?: string; difficulty?: string; keyword?: string },
): Promise<{ approved: number }> {
  return request(`/api/${domainId}/admin/assessment/items/approve-all`, {
    method: 'POST',
    body: JSON.stringify(filters),
  });
}

// 기출 PDF 업로드 → 그림 crop + draft item 일괄 생성 (ADR-025 §2)
export async function importAssessmentPdf(
  domainId: string,
  file: File,
  opts: {
    item_id_prefix: string;
    answer_page_index?: number;
    default_quality_status?: AssessmentQualityStatus;
    tags?: string;
  },
): Promise<AssessmentImportResult> {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('item_id_prefix', opts.item_id_prefix);
  if (opts.answer_page_index !== undefined && opts.answer_page_index !== null) {
    fd.append('answer_page_index', String(opts.answer_page_index));
  }
  if (opts.default_quality_status) {
    fd.append('default_quality_status', opts.default_quality_status);
  }
  if (opts.tags) fd.append('tags', opts.tags);
  return requestForm(`/api/${domainId}/admin/assessment/import`, fd);
}

export async function getReviewQueue(
  domainId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<AssessmentListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${domainId}/admin/assessment/review-queue?${qs}`);
}

export async function getAssessmentAnalytics(
  domainId: string,
): Promise<AssessmentAnalytics> {
  return request(`/api/${domainId}/admin/assessment/analytics`);
}

export async function extractAssessment(
  domainId: string,
  body: {
    subject?: string;
    chapter?: string;
    difficulty_distribution?: Record<string, number>;
    count: number;
    exclude_recent_days?: number;
    tags_any?: string[];
  },
): Promise<AssessmentExtractResult> {
  return request(`/api/${domainId}/assessment/extract`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function generateAssessment(
  domainId: string,
  body: { subject: string; chapter?: string; count: number; difficulty?: string },
): Promise<AssessmentExtractResult> {
  return request(`/api/${domainId}/assessment/generate`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function hybridAssessment(
  domainId: string,
  body: {
    extract: {
      subject?: string;
      chapter?: string;
      count: number;
      difficulty_distribution?: Record<string, number>;
      exclude_recent_days?: number;
      tags_any?: string[];
    };
    generate: {
      subject: string;
      chapter?: string;
      count: number;
      difficulty?: string;
    };
  },
): Promise<AssessmentExtractResult> {
  return request(`/api/${domainId}/assessment/hybrid`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Platform Admin (ADR-017 §18)
// ============================================================================

export async function listTenants(
  params: { status?: TenantStatus; page?: number; page_size?: number } = {},
): Promise<TenantListResult> {
  const qs = new URLSearchParams();
  if (params.status) qs.set('status', params.status);
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/platform/admin/tenants?${qs}`);
}

export async function registerTenant(body: {
  domain_id: string;
  display_name: string;
  domain_type: string;
  embedding_model?: string;
  modules?: string[];
}): Promise<TenantRow> {
  return request('/api/platform/admin/tenants', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function getTenant(domainId: string): Promise<TenantRow> {
  return request(`/api/platform/admin/tenants/${domainId}`);
}

export async function patchTenantStatus(
  domainId: string,
  status: TenantStatus,
  reason?: string,
): Promise<TenantRow> {
  return request(`/api/platform/admin/tenants/${domainId}`, {
    method: 'PATCH',
    body: JSON.stringify({ status, reason }),
  });
}

export async function hardDeleteTenant(
  domainId: string,
  reason: string,
): Promise<{ domain_id: string; status: string; partial: boolean }> {
  return request(`/api/platform/admin/tenants/${domainId}/hard`, {
    method: 'DELETE',
    body: JSON.stringify({ reason }),
  });
}

export async function listPlatformEndpoints(): Promise<{ items: EndpointHealthRow[] }> {
  return request('/api/platform/admin/endpoints');
}

// ADR-022 §3·§4 — 도메인 멤버십 관리 (admin/platform_admin 전역).

export async function listDomainMembers(
  domainId: string,
): Promise<{ domain_id: string; members: DomainMember[] }> {
  return request(`/api/platform/admin/domains/${domainId}/members`);
}

export async function assignDomainMember(
  domainId: string,
  body: {
    user_id: string;
    clearance?: string;
    department?: string | null;
    domain_groups?: string[];
  },
): Promise<DomainMember> {
  return request(`/api/platform/admin/domains/${domainId}/members`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function revokeDomainMember(
  domainId: string,
  userId: string,
): Promise<{ revoked: boolean }> {
  return request(
    `/api/platform/admin/domains/${domainId}/members/${encodeURIComponent(userId)}`,
    { method: 'DELETE' },
  );
}

export async function setDomainEnrollmentPolicy(
  domainId: string,
  enrollmentPolicy: 'open' | 'assigned',
): Promise<{ domain_id: string; enrollment_policy: string }> {
  return request(`/api/platform/admin/domains/${domainId}/enrollment-policy`, {
    method: 'PATCH',
    body: JSON.stringify({ enrollment_policy: enrollmentPolicy }),
  });
}

export async function getPlatformUsage(): Promise<{ items: PlatformUsageRow[]; total: number }> {
  return request('/api/platform/admin/analytics/usage');
}

export async function getPlatformHealth(): Promise<{ items: EndpointHealthRow[] }> {
  return request('/api/platform/admin/analytics/health');
}

export async function getPlatformConfig(
  category: string,
): Promise<{ category: string; value: Record<string, unknown>; exists: boolean }> {
  return request(`/api/platform/admin/configs/${category}`);
}

export async function putPlatformConfig(
  category: string,
  body: { value: Record<string, unknown>; reason?: string },
): Promise<{ category: string }> {
  return request(`/api/platform/admin/configs/${category}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

export async function getHealthMetrics(): Promise<{
  ledger: { publish_failures_total: number; dead_letter_count: number; recent_dead_letters: unknown[] };
  chat_log_writer: { write_failures_total: number; dead_letter_count: number; recent_dead_letters: unknown[] };
}> {
  return request('/api/platform/admin/health/metrics');
}
