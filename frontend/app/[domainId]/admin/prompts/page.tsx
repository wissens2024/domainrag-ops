/**
 * Prompt Studio — /{tid}/admin/prompts (ADR-016 §3.6 + ADR-017 §12).
 *
 * 목록 + 편집 (Monaco) + preview.
 */
'use client';

import dynamic from 'next/dynamic';
import { useParams } from 'next/navigation';
import { useTheme } from 'next-themes';
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import { listPrompts, patchPrompt, previewPrompt } from '@/lib/api';
import type { PromptListResult, PromptRecord } from '@/lib/types';

const Editor = dynamic(() => import('@monaco-editor/react').then((m) => m.default), {
  ssr: false,
  loading: () => <div className="p-4 text-gray-500 dark:text-slate-400">에디터 로딩...</div>,
});

export default function PromptStudioPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const { resolvedTheme } = useTheme();
  const editorTheme = resolvedTheme === 'dark' ? 'vs-dark' : 'light';
  const [selected, setSelected] = useState<PromptRecord | null>(null);
  const [systemText, setSystemText] = useState('');
  const [userText, setUserText] = useState('');
  const [previewQ, setPreviewQ] = useState('질문 예시');
  const [previewResult, setPreviewResult] = useState<unknown>(null);
  const [invokeLlm, setInvokeLlm] = useState(false);
  const [savingMessage, setSavingMessage] = useState<string | null>(null);

  const { data, mutate: refresh } = useSWR<PromptListResult>(
    domainId ? `prompts:${domainId}` : null,
    () => listPrompts(domainId),
  );

  useEffect(() => {
    if (data?.items.length && !selected) {
      setSelected(data.items[0]);
    }
  }, [data, selected]);

  useEffect(() => {
    if (selected) {
      setSystemText(selected.system);
      setUserText(selected.user);
    }
  }, [selected]);

  const handleSave = async () => {
    if (!selected) return;
    setSavingMessage(null);
    try {
      await patchPrompt(
        domainId,
        selected.task,
        selected.version,
        selected.ab_slot,
        {
          system: systemText !== selected.system ? systemText : undefined,
          user: userText !== selected.user ? userText : undefined,
          reason: 'admin edit',
        },
      );
      setSavingMessage('저장 완료. 다음 chat 호출에 즉시 반영.');
      void refresh();
    } catch (e) {
      setSavingMessage(`저장 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  const handlePreview = async () => {
    if (!selected) return;
    try {
      const r = await previewPrompt(domainId, selected.task, {
        system: systemText,
        user: userText,
        sample_question: previewQ,
        invoke_llm: invokeLlm,
      });
      setPreviewResult(r);
    } catch (e) {
      alert(`preview 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">Prompt Studio</h1>

      <div className="grid grid-cols-12 gap-4">
        <Card className="col-span-3" padded={false}>
        <aside className="p-2">
          <p className="text-xs font-bold mb-2 text-gray-500 dark:text-slate-400">prompts</p>
          <ul className="space-y-1 text-sm">
            {data?.items.map((p) => (
              <li
                key={`${p.task}/${p.version}/${p.ab_slot}`}
                onClick={() => setSelected(p)}
                className={`px-2 py-1 rounded cursor-pointer ${
                  selected?.task === p.task &&
                  selected.version === p.version &&
                  selected.ab_slot === p.ab_slot
                    ? 'bg-blue-100 text-blue-900 dark:bg-blue-900/30 dark:text-blue-200'
                    : 'hover:bg-gray-50 dark:hover:bg-slate-700/60'
                }`}
              >
                <div className="font-medium">{p.task}</div>
                <div className="text-xs text-gray-500 dark:text-slate-400">
                  {p.version}/{p.ab_slot} · {p.source}
                </div>
              </li>
            ))}
          </ul>
        </aside>
        </Card>

        <div className="col-span-9 space-y-4">
          {selected && (
            <>
              <div>
                <p className="text-sm font-bold mb-1 text-gray-900 dark:text-slate-100">system</p>
                <div className="border border-gray-200 dark:border-slate-700 rounded overflow-hidden" style={{ height: '160px' }}>
                  <Editor
                    language="markdown"
                    theme={editorTheme}
                    value={systemText}
                    onChange={(v) => setSystemText(v ?? '')}
                    options={{ minimap: { enabled: false }, fontSize: 13, wordWrap: 'on' }}
                  />
                </div>
              </div>
              <div>
                <p className="text-sm font-bold mb-1 text-gray-900 dark:text-slate-100">user</p>
                <div className="border border-gray-200 dark:border-slate-700 rounded overflow-hidden" style={{ height: '200px' }}>
                  <Editor
                    language="markdown"
                    theme={editorTheme}
                    value={userText}
                    onChange={(v) => setUserText(v ?? '')}
                    options={{ minimap: { enabled: false }, fontSize: 13, wordWrap: 'on' }}
                  />
                </div>
              </div>
              <div className="flex gap-2 items-center">
                <Button onClick={handleSave}>
                  저장
                </Button>
                <input
                  value={previewQ}
                  onChange={(e) => setPreviewQ(e.target.value)}
                  placeholder="sample question"
                  className="px-2 py-1 border rounded text-sm flex-1"
                />
                <label className="text-xs">
                  <input
                    type="checkbox"
                    checked={invokeLlm}
                    onChange={(e) => setInvokeLlm(e.target.checked)}
                    className="mr-1"
                  />
                  LLM 호출
                </label>
                <Button variant="secondary" onClick={handlePreview}>
                  ▶ preview
                </Button>
              </div>
              {savingMessage && (
                <p className="text-sm text-gray-700 dark:text-slate-300">{savingMessage}</p>
              )}
              {previewResult ? (
                <pre className="bg-gray-50 dark:bg-slate-900/50 border border-gray-200 dark:border-slate-700 rounded p-3 text-xs overflow-x-auto max-h-96 text-gray-700 dark:text-slate-300">
                  {JSON.stringify(previewResult, null, 2)}
                </pre>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
