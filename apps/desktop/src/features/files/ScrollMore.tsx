import { useEffect, useRef } from 'react';
/** Automatically advances while the end is visible; button also supports keyboards. */
export function ScrollMore({ loading, onMore }: { loading: boolean; onMore: () => void }) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element || loading || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) onMore();
    }, { root: element.closest('.files-list-scroll, .files-parent-rows'), rootMargin: '300px' });
    observer.observe(element);
    return () => observer.disconnect();
  }, [loading, onMore]);
  return <button ref={ref} className="files-scroll-more" disabled={loading} onClick={onMore}>{loading ? 'Loading more…' : 'Load more'}</button>;
}
