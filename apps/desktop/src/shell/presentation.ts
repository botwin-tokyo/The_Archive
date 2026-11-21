import { useEffect, useState } from 'react';

const prefix = 'archive-console.presentation.v1.';

export function readPresentationNumber(key: string, fallback: number, min: number, max: number) {
  try {
    const stored = localStorage.getItem(prefix + key);
    if (stored === null) return fallback;
    const value = Number(stored);
    return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
  } catch { return fallback; }
}

export function usePresentationNumber(key: string, fallback: number, min: number, max: number) {
  const [value, setValue] = useState(() => readPresentationNumber(key, fallback, min, max));
  useEffect(() => { try { localStorage.setItem(prefix + key, String(value)); } catch { /* Preferences are optional. */ } }, [key, value]);
  return [value, (next: number) => setValue(Math.min(max, Math.max(min, next)))] as const;
}

export function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => typeof window.matchMedia === 'function' ? window.matchMedia(query).matches : false);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [query]);
  return matches;
}
