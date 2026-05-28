/**
 * Schema Editor — /{tid}/admin/schema (ADR-016 §3.4 + ADR-015 + ADR-017 §15).
 *
 * Monaco YAML editor + history + optimistic lock 충돌 UI (Y8).
 */
'use client';

import dynamic from 'next/dynamic';
import { useParams } from 'next/navigation';
import { useTheme } from 'next-themes';
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import {
  getSchema,
  getSchemaHistory,
  putSchema,
} from '@/lib/api';
import type { InputSchemaHistory, InputSchemaRecord } from '@/lib/types';

// Monaco는 SSR 미호환 — dynamic import
const Editor = dynamic(() => import('@monaco-editor/react').then((m) => m.default), {
  ssr: false,
  loading: () => <div className="p-4 text-gray-500 dark:text-slate-400">에디터 로딩 중...</div>,
});

export default function SchemaEditorPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const { resolvedTheme } = useTheme();
  const editorTheme = resolvedTheme === 'dark' ? 'vs-dark' : 'light';
  const [yamlText, setYamlText] = useState('');
  const [baseVersion, setBaseVersion] = useState<string | undefined>(undefined);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<{ type: 'ok' | 'error'; text: string } | null>(null);
  const [showHistory, setShowHistory] = useState(false);

  const { data: current, mutate: refresh } = useSWR<InputSchemaRecord>(
    domainId ? `schema:${domainId}` : null,
    () => getSchema(domainId).catch(() => null as unknown as InputSchemaRecord),
  );

  const { data: history } = useSWR<InputSchemaHistory>(
    domainId && showHistory ? `schema-history:${domainId}` : null,
    () => getSchemaHistory(domainId, { page_size: 20 }),
  );

  useEffect(() => {
    if (current) {
      setYamlText(JSON.stringify(current.schema_yaml || {}, null, 2));
      setBaseVersion(current.schema_version);
    }
  }, [current]);

  const handleSave = async () => {
    setSubmitting(true);
    setMessage(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(yamlText);
    } catch (e) {
      setMessage({
        type: 'error',
        text: `JSON 파싱 실패: ${e instanceof Error ? e.message : ''}`,
      });
      setSubmitting(false);
      return;
    }
    try {
      const result = await putSchema(domainId, {
        schema_yaml: parsed,
        base_version: baseVersion,
      });
      setMessage({
        type: 'ok',
        text: `저장 성공. 새 schema_version=${result.record.schema_version} (이전 ${result.deprecated_version}이 deprecated)`,
      });
      void refresh();
    } catch (e) {
      if (e instanceof Error && e.message === 'schema_version_conflict') {
        setMessage({
          type: 'error',
          text: '다른 사용자가 먼저 저장했습니다 (Y8 optimistic lock). 새로고침 후 변경분을 다시 적용하세요.',
        });
      } else if (e instanceof Error && e.message.includes('schema_invalid')) {
        setMessage({
          type: 'error',
          text: `스키마 검증 실패: ${e.message}`,
        });
      } else {
        setMessage({
          type: 'error',
          text: `저장 실패: ${e instanceof Error ? e.message : ''}`,
        });
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">Schema Editor</h1>
          {current && (
            <p className="text-sm text-gray-500 dark:text-slate-400">
              현재 active version: <code>{current.schema_version}</code>
            </p>
          )}
        </div>
        <Button variant="secondary" size="sm" onClick={() => setShowHistory(!showHistory)}>
          {showHistory ? '편집기로' : '이력 보기'}
        </Button>
      </div>

      {!showHistory ? (
        <>
          <div className="border border-gray-200 dark:border-slate-700 rounded overflow-hidden mb-4" style={{ height: '500px' }}>
            <Editor
              language="json"
              theme={editorTheme}
              value={yamlText}
              onChange={(v) => setYamlText(v ?? '')}
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                tabSize: 2,
              }}
            />
          </div>

          {message && (
            <div
              className={`mb-3 p-3 rounded text-sm ${
                message.type === 'ok'
                  ? 'bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                  : 'bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300'
              }`}
            >
              {message.text}
            </div>
          )}

          <div className="flex gap-2">
            <Button onClick={handleSave} disabled={submitting}>
              {submitting ? '저장 중...' : '저장'}
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                if (current) {
                  setYamlText(JSON.stringify(current.schema_yaml || {}, null, 2));
                }
              }}
            >
              초기화
            </Button>
          </div>
        </>
      ) : (
        <div className="space-y-3">
          {history?.items.map((r) => (
            <details
              key={r.schema_version}
              className="border border-gray-200 dark:border-slate-700 rounded p-3 text-sm"
            >
              <summary className="cursor-pointer flex justify-between">
                <span className="font-bold">v{r.schema_version}</span>
                <span className="text-gray-500 dark:text-slate-400">
                  {r.status} · {new Date(r.created_at).toLocaleString('ko-KR')}
                </span>
              </summary>
              <pre className="mt-2 bg-gray-50 dark:bg-slate-900/50 p-2 text-xs overflow-x-auto max-h-60">
                {JSON.stringify(r.schema_yaml, null, 2)}
              </pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
