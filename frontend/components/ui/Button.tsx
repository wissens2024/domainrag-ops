'use client';

import { forwardRef, ButtonHTMLAttributes } from 'react';
import { twMerge } from 'tailwind-merge';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';
type Size = 'sm' | 'md' | 'lg';

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

const VARIANTS: Record<Variant, string> = {
  primary:
    'bg-gray-900 text-white hover:bg-gray-800 active:bg-gray-700 disabled:bg-gray-300 disabled:text-gray-500 ' +
    'dark:bg-brand-600 dark:hover:bg-brand-500 dark:active:bg-brand-700 dark:text-white dark:disabled:bg-slate-700 dark:disabled:text-slate-500',
  secondary:
    'bg-white text-gray-900 border border-gray-300 hover:bg-gray-50 active:bg-gray-100 disabled:bg-gray-50 disabled:text-gray-400 ' +
    'dark:bg-slate-800 dark:text-slate-100 dark:border-slate-600 dark:hover:bg-slate-700 dark:active:bg-slate-600 dark:disabled:bg-slate-800 dark:disabled:text-slate-500',
  ghost:
    'text-gray-700 hover:bg-gray-100 active:bg-gray-200 disabled:text-gray-400 disabled:hover:bg-transparent ' +
    'dark:text-slate-300 dark:hover:bg-slate-700 dark:active:bg-slate-600 dark:disabled:text-slate-600',
  danger:
    'bg-red-600 text-white hover:bg-red-700 active:bg-red-800 disabled:bg-red-300 dark:disabled:bg-red-900/50',
};

const SIZES: Record<Size, string> = {
  sm: 'px-3 py-1.5 text-xs rounded-lg',
  md: 'px-4 py-2 text-sm rounded-lg',
  lg: 'px-5 py-2.5 text-sm rounded-xl',
};

const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  { variant = 'primary', size = 'md', loading, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={twMerge(
        'inline-flex items-center justify-center gap-1.5 font-medium transition-colors',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 focus-visible:ring-offset-2',
        'disabled:cursor-not-allowed',
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading && (
        <span className="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin" />
      )}
      {children}
    </button>
  );
});

export default Button;
