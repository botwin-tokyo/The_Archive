import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { createMockBackend } from '../../adapters/mock';
import { domainError } from '../../contracts';
import type { BackendPorts, ServiceStatus } from '../../contracts';
import { ConnectionsView } from './ConnectionsView';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
async function loaded(ports: BackendPorts) { const result = render(<ConnectionsView ports={ports}/>); await screen.findByRole('article', { name: 'Memory Harness demo identity' }); return result; }
describe('ConnectionsView', () => {
  it('shows independent unconfigured services, sessions, and nonfunctional credentials', async () => {
    const backend = createMockBackend(); const fetch = vi.fn(); const socket = vi.fn(); vi.stubGlobal('fetch', fetch); vi.stubGlobal('WebSocket', socket);
    await loaded(backend);
    for (const name of ['Transfer API', 'MCP server', 'Health WebSocket']) {
      const panel = screen.getByRole('region', { name }); expect(within(panel).getAllByText('Not configured').length).toBeGreaterThan(1); expect(within(panel).getByText('Not connected · demo only')).toBeInTheDocument();
    }
    expect(screen.getByText('2 registered')).toBeInTheDocument(); expect(screen.getByText('0')).toBeInTheDocument();
    expect(screen.getAllByText('DEMO · nonfunctional placeholder')).toHaveLength(2);
    expect(document.querySelector('input[type=password]')).toBeNull(); expect(fetch).not.toHaveBeenCalled(); expect(socket).not.toHaveBeenCalled();
  });
  it('contains repeated telemetry reconnects without changing API, MCP, or agent access', async () => {
    const backend = createMockBackend(); await loaded(backend);
    await act(async () => { backend.setScenario('telemetry-error'); backend.setScenario('telemetry-error'); });
    expect(within(screen.getByRole('region', { name: 'Health WebSocket' })).getByText('Simulated Reconnecting')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Transfer API' })).getAllByText('Not configured')).toHaveLength(3);
    expect(within(screen.getByRole('region', { name: 'MCP server' })).getAllByText('Not configured')).toHaveLength(3);
    expect(screen.getByRole('button', { name: 'Edit scopes for Research agent' })).toBeEnabled();
    expect((await backend.jobs.snapshot()).jobs[0].state).toBe('running');
  });
  it('supports scope editing with logical root validation, cancellation, and identity isolation', async () => {
    const backend = createMockBackend(); const before = await backend.agents.list(); const update = vi.spyOn(backend.agents, 'updateScopes');
    await loaded(backend); fireEvent.click(screen.getByRole('button', { name: 'Edit scopes for Research agent' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'write' })); fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(update).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Edit scopes for Research agent' })); expect(screen.getByRole('checkbox', { name: 'write' })).not.toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: 'write' }));
    const path = screen.getByRole('textbox', { name: 'Path within File Storage' });
    fireEvent.change(path, { target: { value: '../escape' } }); expect(screen.getByRole('button', { name: 'Save demo scopes' })).toBeDisabled();
    fireEvent.change(path, { target: { value: 'Archive/Photography' } }); fireEvent.click(screen.getByRole('button', { name: 'Save demo scopes' }));
    await screen.findByText(/Research agent: demo scopes updated/);
    const after = await backend.agents.list(); expect(after[0]).toEqual(before[0]); expect(after[1].scopes).toEqual(['read', 'write']); expect(after[1].allowedRoots).toEqual([{ driveId: 'demo-files', path: 'Archive/Photography' }]); expect(update).toHaveBeenCalledTimes(1);
  });
  it('requires a revocation decision and affects only the selected mock identity', async () => {
    const backend = createMockBackend(); const revoke = vi.spyOn(backend.agents, 'revoke');
    await loaded(backend); fireEvent.click(screen.getByRole('button', { name: 'Revoke Memory Harness' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('only Memory Harness (agent-harness)'); fireEvent.click(screen.getByRole('button', { name: 'Keep identity' })); expect(revoke).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Revoke Memory Harness' })); fireEvent.click(screen.getByRole('button', { name: 'Revoke demo identity' }));
    await screen.findByText(/Memory Harness: demo identity revoked/);
    expect((await backend.agents.list()).map(agent => agent.revoked)).toEqual([true, false]); expect(screen.getByRole('button', { name: 'Revoke Memory Harness' })).toBeDisabled(); expect(screen.getByRole('button', { name: 'Revoke Research agent' })).toBeEnabled();
    expect((await backend.status.get()).backend).toBe('connected');
  });
  it('reconciles an uncertain revoke by reloading rather than sending it again', async () => {
    const backend = createMockBackend(); const original = backend.agents.revoke;
    const revoke = vi.spyOn(backend.agents, 'revoke').mockImplementation(async (id, revision) => { await original(id, revision); throw domainError('TIMEOUT', 'SECRET_ERROR'); });
    await loaded(backend); fireEvent.click(screen.getByRole('button', { name: 'Revoke Memory Harness' })); fireEvent.click(screen.getByRole('button', { name: 'Revoke demo identity' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('no automatic retry'); expect(document.body.textContent).not.toContain('SECRET_ERROR');
    fireEvent.click(screen.getByRole('button', { name: 'Reload agents' }));
    await screen.findByText('Revoked · demo'); expect(revoke).toHaveBeenCalledTimes(1); expect(screen.getByRole('button', { name: 'Revoke Memory Harness' })).toBeDisabled();
  });
  it('contains owner-only agent load failures while service panels stay available', async () => {
    const backend = createMockBackend(); const ports: BackendPorts = { ...backend, agents: { ...backend.agents, list: async () => { throw domainError('PERMISSION_DENIED', 'SECRET_DENIAL'); } } };
    render(<ConnectionsView ports={ports}/>); expect(await screen.findByRole('alert')).toHaveTextContent('Owner access is required');
    expect(screen.getByRole('region', { name: 'Transfer API' })).toBeInTheDocument(); expect(screen.queryByRole('button', { name: /Revoke / })).not.toBeInTheDocument(); expect(document.body.textContent).not.toContain('SECRET_DENIAL');
  });
  it('ignores old status snapshots after newer subscriptions and unsubscribes on unmount', async () => {
    const backend = createMockBackend(); const initial = await backend.status.get(); let resolveOld!: (status: ServiceStatus) => void;
    const ports: BackendPorts = { ...backend, status: { ...backend.status, get: () => new Promise(resolve => { resolveOld = resolve; }) } };
    const result = await loaded(ports);
    await act(async () => { backend.debug.setBackendConnected(false); });
    expect(screen.getByText('Unavailable · simulated')).toBeInTheDocument();
    await act(async () => { resolveOld(initial); });
    expect(screen.getByText('Unavailable · simulated')).toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Revoke Memory Harness' })).toBeDisabled();
    result.unmount(); expect(backend.debug.subscriptionCount).toBe(0);
  });
  it('does not claim an endpoint is unconfigured when its configuration cannot be read', async () => {
    const backend = createMockBackend(); const ports: BackendPorts = { ...backend, configuration: { ...backend.configuration, get: async () => { throw domainError('UNAVAILABLE', 'No read'); } } };
    await loaded(ports); expect(within(screen.getByRole('region', { name: 'Health WebSocket' })).getByText('Unknown')).toBeInTheDocument();
  });
});
