import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { JobEvent, JobPort } from '../../src/contracts';
import { createMockBackend } from '../../src/adapters/mock';
import { FilesView } from '../../src/features/files';
import { TransferQueue } from '../../src/features/transfers';
import { createJobStore } from '../../src/features/transfers/jobStore';
import { FILES_DRIVE_ID } from '../../src/fixtures/drives';

afterEach(cleanup);
it('keeps 100,000-entry listing and 1,000-job history bounded during a mixed modeled stream', async () => {
  const backend = createMockBackend({ scenario: 'job-history', seedJobs: false, largeDirectoryCount: 100_000 });
  const initial = await backend.jobs.snapshot(); const listeners = new Set<(event: JobEvent) => void>();
  const port: JobPort = { ...backend.jobs, snapshot: async () => initial, subscribe: listener => { listeners.add(listener); return () => { listeners.delete(listener); }; } };
  const store = createJobStore(port); store.start(); await store.reconcile();
  await act(async () => { render(<><FilesView port={backend.files} location={{ driveId: FILES_DRIVE_ID, path: 'Large directory' }} onNavigate={vi.fn()} onSelection={vi.fn()}/><TransferQueue store={store}/></>); });
  const nextButton = screen.getByRole('button', { name: 'Next page' });
  const samples: number[] = [];
  for (let tick = 1; tick <= 50; tick++) {
    const event: JobEvent = { schemaVersion: 1, cursor: tick, job: { ...initial.jobs[0], revision: tick + 1, state: 'running', bytesDone: String(tick), capabilities: { pause: true, resume: false, cancel: true, retry: false } } };
    const started = performance.now();
    await act(async () => { for (const listener of listeners) listener(event); if (tick % 5 === 0) fireEvent.click(nextButton); });
    samples.push(performance.now() - started);
    expect(document.querySelectorAll('.files-table tbody tr')).toHaveLength(100);
    expect(document.querySelectorAll('.job-item')).toHaveLength(50);
  }
  expect(store.getSnapshot().jobs).toHaveLength(1000); expect(screen.getByText('1,001–1,100 of 100,000')).toBeTruthy();
  const ordered = [...samples].sort((a, b) => a - b);
  console.info('PERF combined', JSON.stringify({ fileFixtureEntries: 100_000, jobFixtureEntries: 1000, updates: 50, pageNavigations: 10, modeledHz: 5, realTimePacing: false, maxMountedFileRows: 100, maxMountedJobRows: 50, commitP50Ms: ordered[24], commitP95Ms: ordered[47] }));
  store.stop(); backend.dispose();
}, 20_000);
