import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMockBackend } from '../../src/adapters/mock';
import { FilesView } from '../../src/features/files';
import { SettingsView } from '../../src/features/settings';
import { TransferQueue } from '../../src/features/transfers';
import { createJobStore } from '../../src/features/transfers/jobStore';
import { INITIAL_LOCATION } from '../../src/fixtures/drives';

afterEach(cleanup);
describe('repeat lifecycle cleanup', () => {
  it('releases queue/settings subscriptions and aborted directory tasks across twenty cycles', async () => {
    const backend = createMockBackend({ scenario: 'slow-reads' });
    const store = createJobStore(backend.jobs);
    let maximumSubscriptions = 0;
    const started = performance.now();
    for (let cycle = 0; cycle < 20; cycle++) {
      store.start();
      const view = render(<><FilesView port={backend.files} location={INITIAL_LOCATION} onNavigate={vi.fn()} onSelection={vi.fn()}/><SettingsView ports={backend}/><TransferQueue store={store}/></>);
      maximumSubscriptions = Math.max(maximumSubscriptions, backend.debug.subscriptionCount);
      expect(backend.debug.subscriptionCount).toBe(2);
      view.unmount(); store.stop();
      // The store's in-flight snapshot has no abort signal; drain its controlled read.
      await act(async () => { backend.clock.advance(2000); await Promise.resolve(); });
      expect(backend.debug.subscriptionCount).toBe(0);
      expect(backend.clock.pendingCount).toBe(0);
      expect(document.body.querySelectorAll('.files-view,.settings-view,.transfer-queue')).toHaveLength(0);
    }
    console.info('PERF lifecycle', JSON.stringify({ cycles: 20, maximumSubscriptions, subscriptionsAfterEachUnmount: 0, pendingControlledReadsAfterDrain: 0, totalMs: performance.now() - started, heapGrowthClaim: 'not measured; GC/renderer heap qualification pending' }));
    backend.dispose();
  });
});

it('removes dialog keyboard/focus listeners and returns focus through twenty open/close cycles', async () => {
  const { useState } = await import('react');
  const { Dialog } = await import('../../src/shared/ui');
  function Fixture() { const [open, setOpen] = useState(false); return <><button onClick={() => setOpen(true)}>Review mock action</button><Dialog open={open} title="Review mock action" onClose={() => setOpen(false)}><button>Confirm mock action</button></Dialog></>; }
  const { fireEvent, screen } = await import('@testing-library/react');
  const view = render(<Fixture/>);
  const registered = new Map<string, Set<EventListenerOrEventListenerObject>>();
  const originalAdd = document.addEventListener.bind(document);
  const originalRemove = document.removeEventListener.bind(document);
  const addSpy = vi.spyOn(document, 'addEventListener').mockImplementation((type, listener, options) => {
    if (type === 'keydown' || type === 'focusin') { if (!registered.has(type)) registered.set(type, new Set()); registered.get(type)!.add(listener); }
    originalAdd(type, listener, options);
  });
  const removeSpy = vi.spyOn(document, 'removeEventListener').mockImplementation((type, listener, options) => { registered.get(type)?.delete(listener); originalRemove(type, listener, options); });
  const opener = screen.getByRole('button', { name: 'Review mock action' });
  const originalOverflow = document.body.style.overflow;
  try {
    for (let index = 0; index < 20; index++) {
      opener.focus(); fireEvent.click(opener);
      expect(registered.get('keydown')?.size).toBe(1); expect(registered.get('focusin')?.size).toBe(1);
      fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
      expect(screen.queryByRole('dialog')).toBeNull(); expect(document.activeElement).toBe(opener);
      expect(registered.get('keydown')?.size).toBe(0); expect(registered.get('focusin')?.size).toBe(0);
      expect(document.body.style.overflow).toBe(originalOverflow);
    }
    console.info('PERF dialog-cleanup', JSON.stringify({ cycles: 20, keyboardListenersAfterEachClose: 0, focusListenersAfterEachClose: 0, bodyScrollRestored: true, openerFocusRestored: true }));
  } finally { view.unmount(); addSpy.mockRestore(); removeSpy.mockRestore(); }
});
