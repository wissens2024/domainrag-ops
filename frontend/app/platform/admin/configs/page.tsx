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
import { getPlatformConfig, putPlatformConfig } from '@/lib/api';

const Editor = dynamic(() => import('@monaco-editor/react').then((m) => m.default), {
  ssr: false,
  loading: () => <div className="p-4 text-gray-500">에디터 로딩...</div>,
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
  const { data } = useSWR<Record<string, unknown>>(
    swrKey,
    () => getPlatformConfig(category),
  );

  useEffect(() => {
    if (data) setText(JSON.stringify(data, null, 2));
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
      <h1 className="text-2xl font-bold mb-4">Platform Configs</h1>

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
        <span className="text-xs text-gray-500">
          ⚠ 변경은 모든 tenant에 즉시 영향. 신중히 사용.
        </span>
      </div>

      <div className="border rounded overflow-hidden mb-3" style={{ height: '500px' }}>
        <Editor
          language="json"
          theme={editorTheme}
          value={text}
          onChange={(v) => setText(v ?? '')}
          options={{ minimap: { enabled: false }, fontSize: 13, tabSize: 2 }}
        />
      </div>

      {message && <p className="text-sm mb-3">{message}</p>}

      <button
        onClick={handleSave}
        className="px-4 py-2 bg-blue-600 text-white rounded text-sm"
      >
        저장 (platform/{category}.yaml 덮어쓰기)
      </button>
    </div>
  );
}
