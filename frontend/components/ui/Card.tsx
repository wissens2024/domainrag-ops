import { HTMLAttributes } from 'react';
import { twMerge } from 'tailwind-merge';

interface Props extends HTMLAttributes<HTMLDivElement> {
  padded?: boolean;
}

export default function Card({ padded = true, className, children, ...rest }: Props) {
  return (
    <div
      className={twMerge(
        'bg-white border border-gray-200 rounded-2xl shadow-card',
        'dark:bg-slate-800 dark:border-slate-700',
        padded && 'p-5',
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between mb-5">
      <div>
        <h2 className="text-base font-semibold text-gray-900 dark:text-slate-100">{title}</h2>
        {description && (
          <p className="text-xs text-gray-500 dark:text-slate-400 mt-0.5">{description}</p>
        )}
      </div>
      {action}
    </div>
  );
}
