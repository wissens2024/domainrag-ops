import { HTMLAttributes } from 'react';
import { twMerge } from 'tailwind-merge';

type Tone = 'neutral' | 'success' | 'warn' | 'danger' | 'info' | 'brand';

interface Props extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

const TONES: Record<Tone, string> = {
  neutral: 'bg-gray-100 text-gray-700 border-gray-200',
  success: 'bg-green-50 text-green-700 border-green-200',
  warn: 'bg-amber-50 text-amber-700 border-amber-200',
  danger: 'bg-red-50 text-red-700 border-red-200',
  info: 'bg-blue-50 text-blue-700 border-blue-200',
  brand: 'bg-brand-50 text-brand-700 border-brand-200',
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
