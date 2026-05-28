import { HTMLAttributes } from 'react';
import { twMerge } from 'tailwind-merge';

type Tone = 'neutral' | 'success' | 'warn' | 'danger' | 'info' | 'brand';

interface Props extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

const TONES: Record<Tone, string> = {
  neutral: 'bg-gray-100 text-gray-700 border-gray-200 dark:bg-slate-700 dark:text-slate-200 dark:border-slate-600',
  success: 'bg-green-50 text-green-700 border-green-200 dark:bg-green-900/30 dark:text-green-300 dark:border-green-800',
  warn: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-800',
  danger: 'bg-red-50 text-red-700 border-red-200 dark:bg-red-900/30 dark:text-red-300 dark:border-red-800',
  info: 'bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-900/30 dark:text-blue-300 dark:border-blue-800',
  brand: 'bg-brand-50 text-brand-700 border-brand-200 dark:bg-brand-500/20 dark:text-brand-300 dark:border-brand-700',
};

export default function Badge({
  tone = 'neutral',
  className,
  children,
  ...rest
}: Props) {
  return (
    <span
      className={twMerge(
        'inline-flex items-center px-2 py-0.5 text-[11px] font-medium border rounded-md',
        TONES[tone],
        className,
      )}
      {...rest}
    >
      {children}
    </span>
  );
}
