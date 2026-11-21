import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { JobEvent, JobPort } from '../../src/contracts';
import { createMockBackend } from '../../src/adapters/mock';
import { TransferQueue } from '../../src/features/transfers';
import { createJobStore } from '../../src/features/transfers/jobStore';

afterEach(cleanup);
describe('bounded 1,000-job queue', () => {
  it('keeps every history page at fifty rows and visits the final job', async () => {
    const backend = createMockBackend({ scenario: 'job-history', seedJobs: false });
    const store = createJobStore(backend.jobs);
    await store.reconcile(); expect(store.getSnapshot().jobs).toHaveLength(1000);
    const start = performance.now(); render(<TransferQueue store={store} full/>);
    let maxRows = 0;
    const nextButton = screen.getByRole('button', { name: 'Next jobs' });
    for (let page = 0; page < 20; page++) {
      maxRows = Math.max(maxRows, document.querySelectorAll('.job-item').length);
      expect(document.querySelectorAll('[role="listitem"]')).toHaveLength(50);
      expect(screen.getByText(`${page * 50 + 1}–${(page + 1) * 50} of 1000`)).toBeTruthy();
      if (page < 19) fireEvent.click(nextButton);
    }
    expect((screen.getByRole('button', { name: 'Next jobs' }) as HTMLButtonElement).disabled).toBe(true);
    console.info('PERF jobs-history', JSON.stringify({ environment: 'Node/jsdom; no layout or paint', fixtureJobs: 1000, pages: 20, maxMountedRows: maxRows, allPagesMs: performance.now() - start }));
    backend.dispose();
  }, 20_000);
  it('reconciles fifty modeled 5-Hz updates without growing rows, jobs, or subscriptions', async () => {
    const backend = createMockBackend({ scenario: 'job-history', seedJobs: false });
    const initial = await backend.jobs.snapshot();
    const listeners = new Set<(event: JobEvent) => void>();
    const port: JobPort = { ...backend.jobs, snapshot: async () => initial, subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; } };
    const store = createJobStore(port); store.start(); await store.reconcile();
    render(<TransferQueue store={store} full/>);
    const samples: number[] = [];
    for (let index = 1; index <= 50; index++) {
      const event: JobEvent = { schemaVersion: 1, cursor: index, job: { ...initial.jobs[0], revision: index + 1, state: 'running', bytesDone: String(index), capabilities: { pause: true, resume: false, cancel: true, retry: false } } };
      const start = performance.now();
      await act(async () => { for (const listener of listeners) listener(event); });
      samples.push(performance.now() - start);
      expect(document.querySelectorAll('.job-item')).toHaveLength(50);
      expect(store.getSnapshot().jobs).toHaveLength(1000);
    }
    expect(store.getSnapshot().jobs[0].bytesDone).toBe('50'); expect(listeners.size).toBe(1);
    store.stop(); expect(listeners.size).toBe(0);
    const sorted = [...samples].sort((a, b) => a - b);
    console.info('PERF jobs-updates', JSON.stringify({ fixtureJobs: 1000, maxMountedRows: 50, updates: 50, modeledHz: 5, modeledDurationSeconds: 10, realTimePacing: false, commitP50Ms: sorted[24], commitP95Ms: sorted[47], subscriptionsAfterStop: listeners.size }));
    backend.dispose();
  });
});
