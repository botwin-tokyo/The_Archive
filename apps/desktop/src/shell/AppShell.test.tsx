import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppShell } from './AppShell';
import { readPresentationNumber } from './presentation';

const slots = { route: 'files', onRoute: vi.fn(), sidebar: <div>Two independent drives</div>, inspector: <div>Selected file metadata</div>, queue: <div>Synthetic transfer queue</div>, children: <div>Detailed files table</div> };
beforeEach(() => { localStorage.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function media(compact: boolean) {
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => ({ matches: compact, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
}

describe('AppShell', () => {
  it('shows the selected four-region composition with truthful integration states', () => {
    media(false); const onRoute = vi.fn(); render(<AppShell {...slots} onRoute={onRoute}/>);
    for (const text of ['Two independent drives', 'Detailed files table', 'Selected file metadata', 'Synthetic transfer queue', 'DEMO DATA', 'No real storage connected', 'API: Not configured', 'MCP: Not configured', 'Health WS: Not configured']) expect(screen.getByText(text)).toBeTruthy();
    expect(screen.getByRole('main', { name: 'Files browser' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Connections' })); expect(onRoute).toHaveBeenCalledWith('connections');
    expect(screen.queryByText(/API Ready/)).toBeNull();
  });
  it('keeps navigation, inspector, and queue available after the main panel throws', () => {
    media(false); vi.spyOn(console, 'error').mockImplementation(() => undefined);
    function Broken(): never { throw new Error('simulated renderer failure'); }
    render(<AppShell {...slots}><Broken/></AppShell>);
    expect(screen.getByRole('alert').textContent).toContain('Files browser couldn’t load');
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeTruthy();
    expect(screen.getByText('Selected file metadata')).toBeTruthy();
    expect(screen.getByText('Synthetic transfer queue')).toBeTruthy();
  });
  it('collapses the queue without unmounting its content or modifying jobs', () => {
    media(false); render(<AppShell {...slots}/>);
    fireEvent.click(screen.getByRole('button', { name: 'Collapse transfer queue' }));
    expect(screen.getByText('Synthetic transfer queue').closest('[hidden]')).not.toBeNull();
    expect(localStorage.getItem('archive-console.presentation.v1.queue-collapsed')).toBe('1');
    fireEvent.click(screen.getByRole('button', { name: 'Expand transfer queue' }));
    expect(screen.getByText('Synthetic transfer queue').closest('[hidden]')).toBeNull();
  });
  it('uses an accessible inspector drawer below 1280px', () => {
    media(true); render(<AppShell {...slots}/>);
    expect(screen.queryByText('Selected file metadata')).toBeNull();
    const trigger = screen.getByRole('button', { name: 'Open selection inspector' });
    trigger.focus(); fireEvent.click(trigger);
    expect(screen.getByRole('dialog', { name: 'Selection inspector' })).toBeTruthy();
    expect(screen.getByText('Selected file metadata')).toBeTruthy();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull(); expect(document.activeElement).toBe(trigger);
  });
  it('reserves the entire workspace for the Transfers route', () => {
    media(false); render(<AppShell {...slots} route="transfers"/>);
    expect(screen.queryByRole('region', { name: 'Transfer queue' })).toBeNull();
    expect(screen.queryByRole('complementary', { name: 'Selection inspector' })).toBeNull();
  });
});

it('only restores finite, bounded presentation numbers and tolerates blocked storage', () => {
  localStorage.setItem('archive-console.presentation.v1.size', 'Infinity'); expect(readPresentationNumber('size', 310, 280, 400)).toBe(310);
  localStorage.setItem('archive-console.presentation.v1.size', '123000'); expect(readPresentationNumber('size', 310, 280, 400)).toBe(400);
  localStorage.setItem('archive-console.presentation.v1.size', '-19'); expect(readPresentationNumber('size', 310, 280, 400)).toBe(280);
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); }); expect(readPresentationNumber('size', 310, 280, 400)).toBe(310);
});
