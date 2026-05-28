'use client';

import { forwardRef, InputHTMLAttributes } from 'react';
import { twMerge } from 'tailwind-merge';

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  hint?: string;
  error?: string;
}

const Input = forwardRef<HTMLInputElement, Props>(function Input(
  { label, hint, error, className, id, ...rest },
  ref,
) {
  const inputId = id || rest.name;
  return (
    <div className="w-full">
      {label && (
        <label
          htmlFor={inputId}
          className="block text-xs font-medium text-gray-700 dark:text-slate-300 mb-1.5"
        >
          {label}
        </label>
      )}
      <input
        ref={ref}
        id={inputId}
        className={twMerge(
          'w-full px-3 py-2 text-sm bg-white border border-gray-300 rounded-lg',
          'dark:bg-slate-900 dark:border-slate-600 dark:text-slate-100',
          'placeholder:text-gray-400 dark:placeholder:text-slate-500',
          'focus:outline-none focus:border-gray-900 focus:ring-1 focus:ring-gray-900',
          'dark:focus:border-brand-500 dark:focus:ring-brand-500',
          'disabled:bg-gray-50 disabled:text-gray-500 disabled:cursor-not-allowed',
          'dark:disabled:bg-slate-800 dark:disabled:text-slate-500',
          error && 'border-red-500 focus:border-red-500 focus:ring-red-500',
          className,
        )}
        {...rest}
      />
      {hint && !error && (
        <p className="mt-1.5 text-[11px] text-gray-500 dark:text-slate-400">{hint}</p>
      )}
      {error && <p className="mt-1.5 text-[11px] text-red-600">{error}</p>}
    </div>
  );
});

export default Input;
