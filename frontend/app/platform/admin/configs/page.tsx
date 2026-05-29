/**
 * Platform Configs — /platform/admin/configs (ADR-017 §18).
 *
 * platform/<category>.yaml read / write (PLATFORM_ADMIN 전용 키 포함).
 */
'use client';

import dynamic from 'next/dynamic';
import { useTheme } from 'next-themes';
import { useEffect, useState } from 'react';
import useSWR, { mutate } from 'swr';
import Button from '@/components/ui/Button';
import { getPlatformConfig, putPlatformConfig } from '@/lib/api';

const Editor = dynamic(() => import('@monaco-editor/react').then((m) => m.default), {
  ssr: false,
  loading: () => <div className="p-4 text-gray-500 dark:text-slate-400">에디터 로딩...</div>,
});

const CATEGORIES = [
  'citation',
  'retrieval',
  'model',
  'routing',
  'query_classifier',
  'lifecycle',
  'auth',
  'pii',
  'audit',
  'data_retention',
];

export default function PlatformConfigsPage() {
  const { resolvedTheme } = useTheme();
  const editorTheme = resolvedTheme === 'dark' ? 'vs-dark' : 'light';
  const [category, setCategory] = useState('model');
  const [text, setText] = useState('');
  const [message, setMessage] = useState<string | null>(null);

  const swrKey = `platform-config:${category}`;
  const { data } = useSWR(
    swrKey,
    () => getPlatformConfig(category),
  );

  useEffect(() => {
    // 백엔드 응답은 {category, value, exists} 래퍼 — 에디터엔 value(실제 config)만 표시.
    if (data) setText(JSON.stringify(data.value ?? {}, null, 2));
  }, [data]);

  const handleSave = async () => {
    setMessage(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      setMessage(`JSON parse: ${e instanceof Error ? e.message : ''}`);
      return;
    }
    try {
      await putPlatformConfig(category, { value: parsed });
      setMessage('저장 완료. 모든 tenant cache invalidate.');
      void mutate(swrKey);
    } catch (e) {
      setMessage(`저장 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">Platform Configs</h1>

      <div className="mb-3 flex gap-2 items-center text-sm">
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="px-3 py-1 border rounded"
        >
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <span className="text-xs text-gray-500 dark:text-slate-400">
          ⚠ 변경은 모든 tenant에 즉시 영향. 신중히 사용.
        </span>
      </div>

      <div className="border border-gray-200 dark:border-slate-700 rounded overflow-hidden mb-3" style={{ height: '500px' }}>
        <Editor
          language="json"
          theme={editorTheme}
          value={text}
          onChange={(v) => setText(v ?? '')}
          options={{ minimap: { enabled: false }, fontSize: 13, tabSize: 2 }}
        />
      </div>

      {message && <p className="text-sm mb-3 text-gray-700 dark:text-slate-300">{message}</p>}

      <Button onClick={handleSave}>
        저장 (platform/{category}.yaml 덮어쓰기)
      </Button>
    </div>
  );
}
