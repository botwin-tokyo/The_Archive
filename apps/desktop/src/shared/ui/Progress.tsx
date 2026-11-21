export function Progress({ value, label = 'Progress', tone = 'mint', className = '' }: {
  value?: number; label?: string; tone?: 'mint' | 'warning' | 'danger'; className?: string;
}) {
  const known = typeof value === 'number' && Number.isFinite(value);
  const percentage = known ? Math.min(100, Math.max(0, value)) : undefined;
  return <div className={`progress progress--${tone} ${known ? '' : 'progress--indeterminate'} ${className}`}
    role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percentage}
    aria-valuetext={known ? `${Math.round(percentage!)}%` : 'Calculating'}>
    <span style={{ width: known ? `${percentage}%` : '34%' }}/>
  </div>;
}
