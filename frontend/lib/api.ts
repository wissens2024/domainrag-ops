import type { ChatResponse } from './types';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ error: 'request_failed' }));
    throw new Error(detail.error || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function chat(
  tenantId: string,
  body: { question: string; conversation_id?: string; ui_mode_request?: 'structured' | 'streaming' },
): Promise<ChatResponse> {
  return request<ChatResponse>(`/api/${tenantId}/chat`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function postFeedback(
  tenantId: string,
  body: { message_id: string; feedback: 'good' | 'bad'; comment?: string },
): Promise<void> {
  await request(`/api/${tenantId}/feedback`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}
