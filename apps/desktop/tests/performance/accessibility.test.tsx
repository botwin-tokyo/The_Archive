import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMockBackend } from '../../src/adapters/mock';
import { FilesView } from '../../src/features/files';
import { TransferQueue } from '../../src/features/transfers';
import { createJobStore } from '../../src/features/transfers/jobStore';
import { INITIAL_LOCATION } from '../../src/fixtures/drives';

afterEach(cleanup);
function luminance(hex: string): number {
  const channels = hex.match(/[a-f\d]{2}/gi)!.map(value => parseInt(value, 16) / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
  return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
}
describe('automated accessibility evidence, without visual or assistive technology claims', () => {
  it('keeps core token text colors at 4.5:1 on the declared opaque surfaces', () => {
    const source = readFileSync('apps/desktop/src/styles/tokens.css', 'utf8');
    const tokens = Object.fromEntries([...source.matchAll(/--([\w-]+):\s*(#[\da-f]{6});/g)].map(match => [match[1], match[2]]));
    const results: { foreground: string; background: string; ratio: number }[] = [];
    for (const foreground of ['text', 'text-secondary', 'muted', 'mint', 'warning', 'danger']) for (const background of ['bg', 'panel', 'surface', 'surface-raised', 'selected']) {
      const lighter = Math.max(luminance(tokens[foreground]), luminance(tokens[background])); const darker = Math.min(luminance(tokens[foreground]), luminance(tokens[background])); const ratio = (lighter + .05) / (darker + .05);
      results.push({ foreground, background, ratio }); expect(ratio, `${foreground} on ${background}`).toBeGreaterThanOrEqual(4.5);
    }
    const worst = results.sort((a, b) => a.ratio - b.ratio)[0];
    console.info('A11Y token contrast', JSON.stringify({ testedPairs: results.length, minimum: worst, excludes: 'hardcoded colors, opacity, gradients, disabled states, real rendered contrast' }));
  });
  it('focuses folder search with Ctrl+F and does not capture text-field Ctrl+A or Delete as file operations', async () => {
    const backend = createMockBackend(); const operation = vi.fn(); const select = vi.fn();
    render(<FilesView port={backend.files} location={INITIAL_LOCATION} onNavigate={vi.fn()} onSelection={select} onOperation={operation}/>);
    const file = await screen.findByText('Field recordings.wav'); fireEvent.click(file); const row = file.closest('tr')!; act(() => { row.focus(); });
    fireEvent.keyDown(row, { key: 'f', ctrlKey: true }); const search = screen.getByRole('textbox', { name: 'Search names in this folder' }); expect(document.activeElement).toBe(search);
    const prior = select.mock.calls.length;
    fireEvent.keyDown(search, { key: 'a', ctrlKey: true }); fireEvent.keyDown(search, { key: 'Delete' });
    expect(operation).not.toHaveBeenCalled(); expect(select).toHaveBeenCalledTimes(prior); backend.dispose();
  });
  it('keeps unknown byte totals indeterminate and uses row keyboard navigation', async () => {
    const backend = createMockBackend({ scenario: 'unknown-total' }); const store = createJobStore(backend.jobs); await store.reconcile();
    render(<TransferQueue store={store}/>);
    const unknown = screen.getByRole('progressbar', { name: 'harness-demo-002.tar bytes transferred' }); expect(unknown.hasAttribute('aria-valuenow')).toBe(false);
    const rows = [...document.querySelectorAll<HTMLElement>('.job-row')]; rows[0].focus(); fireEvent.keyDown(rows[0], { key: 'ArrowDown' }); expect(document.activeElement).toBe(rows[1]);
    fireEvent.keyDown(rows[1], { key: 'Enter' }); expect(screen.getByText(/Job demo-job-copy/)).toBeTruthy();
    await act(async () => { await store.reconcile(); }); expect(document.activeElement).toBe(rows[1]); backend.dispose();
  });
});
