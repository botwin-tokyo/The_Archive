import type { ReactNode } from 'react';

export function StatusBadge({ children, tone = 'neutral', dot = true, className = '' }: {
  children: ReactNode; tone?: 'neutral' | 'success' | 'warning' | 'danger'; dot?: boolean; className?: string;
}) {
  return <span className={`status-badge status-badge--${tone} ${className}`}>
    {dot && <span className="status-dot" aria-hidden="true"/>}{children}
  </span>;
}
