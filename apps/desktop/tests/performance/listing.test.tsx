import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMockBackend } from '../../src/adapters/mock';
import { FilesView } from '../../src/features/files';
import { FILES_DRIVE_ID } from '../../src/fixtures/drives';

const location = { driveId: FILES_DRIVE_ID, path: 'Large directory' };
const percentile = (values: number[], fraction: number) => [...values].sort((a, b) => a - b)[Math.ceil(values.length * fraction) - 1];
afterEach(cleanup);
describe('bounded 100,000-entry listing', () => {
  it('requests and retains only one 100-row page while advancing twenty pages', async () => {
    const backend = createMockBackend({ seedJobs: false, largeDirectoryCount: 100_000 });
    const list = vi.fn(backend.files.list);
    const cpuStart = process.cpuUsage();
    const started = performance.now();
    await act(async () => { render(<FilesView port={{ ...backend.files, list }} location={location} onNavigate={vi.fn()} onSelection={vi.fn()}/>); });
    const samples: number[] = [];
    const nextButton = screen.getByRole('button', { name: 'Next page' });
    let maxRows = 0;
    for (let page = 0; page < 20; page++) {
      expect(screen.getByText(`Synthetic item ${String(page * 100 + 1).padStart(6, '0')}.bin`)).toBeTruthy();
      const rows = document.querySelectorAll('.files-table tbody tr').length;
      maxRows = Math.max(maxRows, rows);
      expect(rows).toBe(100);
      expect(document.querySelectorAll('table')).toHaveLength(1);
      if (page < 19) {
        const before = performance.now();
        await act(async () => { fireEvent.click(nextButton); });
        samples.push(performance.now() - before);
      }
    }
    expect(list).toHaveBeenCalledTimes(20);
    expect(list.mock.calls.every(([query]) => query.limit === 100)).toBe(true);
    expect(screen.queryByText('Synthetic item 000001.bin')).toBeNull();
    expect(screen.getByText('1,901–2,000 of 100,000')).toBeTruthy();
    const cpu = process.cpuUsage(cpuStart);
    console.info('PERF listing', JSON.stringify({ environment: 'Node/jsdom; no layout or paint', fixtureEntries: 100_000, pages: 20, maxMountedRows: maxRows, navigationSamples: samples.length, navigationP50Ms: percentile(samples, .5), navigationP95Ms: percentile(samples, .95), totalMs: performance.now() - started, processCpuUserMs: cpu.user / 1000, processCpuSystemMs: cpu.system / 1000 }));
    backend.dispose();
  }, 20_000);
  it('generates bounded metadata pages without retaining 100,000 Entry objects', async () => {
    const backend = createMockBackend({ seedJobs: false, largeDirectoryCount: 100_000 });
    const samples: number[] = [];
    let cursor: string | null = null;
    for (let index = 0; index < 30; index++) {
      const start = performance.now();
      const page = await backend.files.list({ location, requestId: `measurement-${index}`, limit: 100, cursor });
      samples.push(performance.now() - start);
      expect(page.entries).toHaveLength(100); expect(page.total).toBe(100_000);
      cursor = page.nextCursor;
    }
    expect(backend.debug.retainedRequestCount).toBe(0);
    console.info('PERF listing-adapter', JSON.stringify({ fixtureEntries: 100_000, samples: samples.length, pageRows: 100, p50Ms: percentile(samples, .5), p95Ms: percentile(samples, .95), retainedMutationRequests: backend.debug.retainedRequestCount }));
    backend.dispose();
  });
});
