const modifiedFormatter = new Intl.DateTimeFormat('en', { month: 'short', day: '2-digit', year: 'numeric', timeZone: 'UTC' });
export function formatBytes(bytes: string | null): string {
  if (bytes === null) return 'Unknown';
  const value = BigInt(bytes);
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let divisor = 1n;
  let unit = 0;
  while (value >= divisor * 1000n && unit < units.length - 1) { divisor *= 1000n; unit++; }
  const tenths = value * 10n / divisor;
  return `${unit === 0 ? value.toString() : `${tenths / 10n}.${tenths % 10n}`} ${units[unit]}`;
}
export function formatModified(value: string | null): string {
  if (!value) return 'Unknown';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Unknown' : modifiedFormatter.format(date);
}
