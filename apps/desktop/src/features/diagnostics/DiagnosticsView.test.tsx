import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { Diagnostic, DiagnosticsPort } from '../../contracts';
import { DiagnosticsView } from './DiagnosticsView';
afterEach(cleanup);
const sample: Diagnostic = { id: 'diagnostic-1', at: '2026-09-30T09:00:00.000Z', level: 'warning', code: 'TIMEOUT', message: 'SECRET_SENTINEL', correlationId: 'mock-1', simulated: true };
describe('DiagnosticsView', () => {
  it('displays only the redacted allowlist and supports local refresh', async () => {
    const list = vi.fn().mockResolvedValue([sample]);
    render(<DiagnosticsView port={{ list }}/>);
    expect(await screen.findByText('A simulated request timed out.')).toBeInTheDocument();
    expect(screen.queryByText('SECRET_SENTINEL')).not.toBeInTheDocument();
    expect(screen.getByText('Correlation: mock-1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh logs' }));
    expect(await screen.findByText('A simulated request timed out.')).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
  });
  it('keeps a stale snapshot on local read failure without printing thrown content', async () => {
    const list = vi.fn().mockResolvedValueOnce([sample]).mockRejectedValue(new Error('SECRET_ERROR'));
    render(<DiagnosticsView port={{ list }}/>); await screen.findByText('TIMEOUT');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh logs' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Showing the last snapshot');
    expect(screen.getByText('TIMEOUT')).toBeInTheDocument(); expect(document.body.textContent).not.toContain('SECRET_ERROR');
  });
  it('ignores a stale response even when a port ignores abort', async () => {
    let resolveOld!: (value: Diagnostic[]) => void;
    const old: DiagnosticsPort = { list: () => new Promise(resolve => { resolveOld = resolve; }) };
    const next: DiagnosticsPort = { list: async () => [] };
    const result = render(<DiagnosticsView port={old}/>); result.rerender(<DiagnosticsView port={next}/>);
    await screen.findByText('No synthetic events recorded.');
    await act(async () => { resolveOld([sample]); });
    expect(screen.queryByText('TIMEOUT')).not.toBeInTheDocument();
  });
});
