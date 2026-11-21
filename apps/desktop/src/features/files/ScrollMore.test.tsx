import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ScrollMore } from './ScrollMore';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it('loads when the end enters the scroll viewport and disconnects during loading', async () => {
  let callback: IntersectionObserverCallback = () => {};
  const disconnect = vi.fn(), observe = vi.fn(), more = vi.fn();
  vi.stubGlobal('IntersectionObserver', class {
    constructor(cb: IntersectionObserverCallback) { callback = cb; }
    observe = observe;
    disconnect = disconnect;
  });
  const view = render(<div className="files-list-scroll"><ScrollMore loading={false} onMore={more}/></div>);
  expect(observe).toHaveBeenCalledWith(screen.getByRole('button'));
  callback([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);
  await waitFor(() => expect(more).toHaveBeenCalledOnce());
  view.rerender(<div className="files-list-scroll"><ScrollMore loading onMore={more}/></div>);
  expect(disconnect).toHaveBeenCalledOnce();
  expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true);
});
