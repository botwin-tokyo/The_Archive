import { useEffect, useRef, useState } from 'react';
import { domainError, safeError, type AppError, type DirectoryPage, type DirectoryQuery, type FileReadPort, type Location } from '../../contracts';
import { locationKey } from './history';

export const FILE_PAGE_SIZE = 100;
export interface DirectoryState { page: DirectoryPage | null; loading: boolean; refreshing: boolean; error: AppError | null }
interface Options { port: FileReadPort; location: Location; sort: DirectoryQuery['sort']; direction: DirectoryQuery['direction']; search: string; refreshKey: number }
export function useDirectory({ port, location, sort, direction, search, refreshKey }: Options): DirectoryState & { loadMore: () => void } {
  const [state, setState] = useState<DirectoryState>({ page: null, loading: true, refreshing: false, error: null });
  const reload = useRef<(more?: boolean) => void>(() => {});
  const lastRefresh = useRef(refreshKey);
  const key = locationKey(location);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false, queued = false, snapshot: DirectoryPage | null = null;
    const read = async (more = false) => {
      if (controller.signal.aborted) return;
      if (inFlight) { if (!more) queued = true; return; }
      if (more && (!snapshot?.nextCursor || snapshot.stale)) return;
      inFlight = true;
      const target = Math.max(FILE_PAGE_SIZE, (snapshot?.entries.length ?? 0) + (more ? FILE_PAGE_SIZE : 0));
      setState(previous => ({ page: snapshot ?? (previous.page && locationKey(previous.page.location) === key ? previous.page : null), loading: true, refreshing: !!snapshot, error: null }));
      try {
        for (let attempt = 0; ; attempt++) {
          try {
            let accumulated = more && attempt === 0 ? snapshot : null;
            let cursor = accumulated?.nextCursor ?? null;
            const seen = new Set<string>();
            do {
              const requestId = crypto.randomUUID();
              const page = await port.list({ location, requestId, cursor, limit: FILE_PAGE_SIZE, sort, direction, foldersFirst: true, search, scope: 'directory' }, { signal: controller.signal });
              if (controller.signal.aborted) return;
              if (page.requestId !== requestId || locationKey(page.location) !== key) throw domainError('INVALID_RESPONSE', 'The folder response did not match this location. Retry the listing.');
              if (page.stale) throw domainError('STALE_REVISION', 'The folder listing is stale. Refresh to try again.', true);
              const entries = new Map((accumulated?.entries ?? []).map(entry => [entry.id, entry]));
              for (const entry of page.entries) entries.set(entry.id, entry);
              accumulated = { ...page, entries: [...entries.values()] };
              cursor = page.nextCursor;
              if (cursor && seen.has(cursor)) throw domainError('INVALID_RESPONSE', 'The folder returned a repeated listing cursor. Retry the listing.');
              if (cursor) seen.add(cursor);
            } while (cursor && accumulated.entries.length < target);
            snapshot = accumulated;
            setState({ page: snapshot, loading: false, refreshing: false, error: null });
            break;
          } catch (error: unknown) {
            if (controller.signal.aborted) return;
            const code = safeError(error).code;
            // Expired/evicted cursors are rebuilt to the loaded depth, never appended
            // to a different snapshot. This also preserves scroll depth on refresh.
            if (attempt >= 2 || (code !== 'STALE_REVISION' && code !== 'SOURCE_CHANGED')) throw error;
          }
        }
      } catch (error: unknown) {
        if (!controller.signal.aborted) {
          if (snapshot) snapshot = { ...snapshot, stale: true };
          setState(previous => ({ page: previous.page ? { ...previous.page, stale: true } : null, loading: false, refreshing: false, error: safeError(error) }));
        }
      } finally {
        inFlight = false;
        if (queued && !controller.signal.aborted) { queued = false; void read(); }
      }
    };
    reload.current = more => { void read(more); };
    void read();
    return () => controller.abort();
  }, [port, key, sort, direction, search]);
  useEffect(() => {
    if (lastRefresh.current !== refreshKey) { lastRefresh.current = refreshKey; reload.current(); }
  }, [refreshKey]);
  return { ...state, loadMore: () => reload.current(true) };
}
