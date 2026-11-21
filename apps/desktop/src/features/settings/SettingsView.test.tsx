import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, waitFor, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { createMockBackend } from '../../adapters/mock';
import type { BackendPorts, Configuration } from '../../contracts';
import { domainError } from '../../contracts';
import { SettingsView } from './SettingsView';
import { endpointHasPrivateParts, validateSettingsDraft } from './draft';
afterEach(cleanup);
const labelInput = () => screen.getByRole('textbox', { name: 'Storage label' });
const saveButton = () => screen.getByRole('button', { name: 'Save demo settings' });
async function loaded(ports: BackendPorts, props: { onDirtyChange?: (dirty: boolean) => void; onSaved?: () => void } = {}) { const result = render(<SettingsView ports={ports} {...props}/>); await screen.findByRole('textbox', { name: 'Storage label' }); return result; }
describe('SettingsView', () => {
  it('keeps settings local until explicit save, signals dirty navigation, and cancels cleanly', async () => {
    const backend = createMockBackend(); const dirty = vi.fn(); const saved = vi.fn(); const save = vi.spyOn(backend.configuration, 'save');
    await loaded(backend, { onDirtyChange: dirty, onSaved: saved });
    fireEvent.change(labelInput(), { target: { value: 'Draft storage' } });
    expect(dirty).toHaveBeenLastCalledWith(true); expect((await backend.configuration.get()).driveLabels['demo-files']).toBe('File Storage');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel changes' }));
    expect(labelInput()).toHaveValue('File Storage'); expect(dirty).toHaveBeenLastCalledWith(false); expect(save).not.toHaveBeenCalled();
    fireEvent.change(labelInput(), { target: { value: 'Saved storage' } }); fireEvent.click(saveButton());
    await screen.findByText('Demo settings saved for this session. No connection or restart was performed.');
    expect((await backend.configuration.get()).driveLabels['demo-files']).toBe('Saved storage'); await waitFor(() => expect(dirty).toHaveBeenLastCalledWith(false)); expect(saved).toHaveBeenCalledTimes(1);
  });
  it('rejects invalid, real, and credential-bearing endpoint syntax without creating a connection', async () => {
    const backend = createMockBackend(); const save = vi.spyOn(backend.configuration, 'save'); const test = vi.spyOn(backend.configuration, 'testHealth');
    await loaded(backend); const endpoint = screen.getByRole('textbox', { name: 'Synthetic endpoint' });
    fireEvent.change(endpoint, { target: { value: 'https://monitor.example.invalid' } }); expect(saveButton()).toBeDisabled();
    fireEvent.change(endpoint, { target: { value: 'wss://example.com/health' } }); expect(screen.getByText(/For this demo, use a reserved/)).toBeInTheDocument(); expect(saveButton()).toBeDisabled();
    fireEvent.change(endpoint, { target: { value: 'wss://owner:SECRET_SENTINEL@example.invalid?token=SECRET_SENTINEL' } });
    expect(endpoint).toHaveValue('wss://example.com/health'); expect(document.body.textContent).not.toContain('SECRET_SENTINEL'); expect(saveButton()).toBeDisabled();
    fireEvent.change(endpoint, { target: { value: 'wss://monitor.example.invalid/health' } }); fireEvent.click(saveButton());
    await screen.findByText('Demo settings saved for this session. No connection or restart was performed.');
    expect(save).toHaveBeenCalledTimes(1); expect(test).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Simulate connection test' }));
    expect(await screen.findByText('Simulated connection test passed. No network connection was made.')).toBeInTheDocument();
  });
  it('preserves refused drafts without claiming success', async () => {
    const backend = createMockBackend({ scenario: 'settings-rejected' }); const saved = vi.fn();
    await loaded(backend, { onSaved: saved }); fireEvent.change(labelInput(), { target: { value: 'Keep this draft' } }); fireEvent.click(saveButton());
    await screen.findByText('Save refused: owner permission is required. Your draft is preserved.');
    expect(labelInput()).toHaveValue('Keep this draft'); expect(saved).not.toHaveBeenCalled(); expect(saveButton()).toBeEnabled();
  });
  it('blocks repeat save after a timeout and reconciles before an explicit retry', async () => {
    const backend = createMockBackend({ scenario: 'settings-timeout' }); const save = vi.spyOn(backend.configuration, 'save');
    await loaded(backend); fireEvent.change(labelInput(), { target: { value: 'Uncertain draft' } }); fireEvent.click(saveButton());
    await screen.findByText(/Save timed out. The outcome is uncertain/); expect(labelInput()).toHaveValue('Uncertain draft'); expect(saveButton()).toBeDisabled();
    fireEvent.click(saveButton()); expect(save).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Check saved state' }));
    await screen.findByText(/Saved state checked: your changes were not saved/); expect(saveButton()).toBeEnabled(); expect(save).toHaveBeenCalledTimes(1);
  });
  it('recognizes a timed-out save that committed without submitting twice', async () => {
    const backend = createMockBackend(); const realSave = backend.configuration.save;
    const save = vi.spyOn(backend.configuration, 'save').mockImplementation(async draft => { await realSave(draft); throw domainError('TIMEOUT', 'SECRET_TIMEOUT'); });
    await loaded(backend); fireEvent.change(labelInput(), { target: { value: 'Committed draft' } }); fireEvent.click(saveButton());
    await screen.findByText(/Save timed out/); fireEvent.click(screen.getByRole('button', { name: 'Check saved state' }));
    await screen.findByText('The saved snapshot matches your draft. No repeat save was sent.');
    expect(save).toHaveBeenCalledTimes(1); expect(saveButton()).toBeDisabled(); expect(document.body.textContent).not.toContain('SECRET_TIMEOUT');
  });
  it('requires explicit stale-revision review while preserving the draft', async () => {
    const backend = createMockBackend(); await loaded(backend);
    fireEvent.change(labelInput(), { target: { value: 'My pending label' } });
    const latest = await backend.configuration.get(); await act(async () => { await backend.configuration.save({ ...latest, transferConcurrency: 4 }); });
    fireEvent.click(saveButton()); await screen.findByText(/Saved settings changed elsewhere/);
    fireEvent.click(screen.getByRole('button', { name: 'Check saved state' }));
    const review = await screen.findByRole('region', { name: 'Latest saved settings' });
    expect(within(review).getByText('4')).toBeInTheDocument(); expect(labelInput()).toHaveValue('My pending label'); expect(saveButton()).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Use saved settings' }));
    expect(labelInput()).toHaveValue('File Storage'); expect(screen.getByRole('combobox', { name: /Concurrent transfers/ })).toHaveValue('4'); expect(saveButton()).toBeDisabled();
  });
  it('shows unknown hardware, destination-only Harness language, and deferred restart', async () => {
    const backend = createMockBackend(); await loaded(backend);
    expect(screen.getByText('Memory Harness owns the schedule')).toBeInTheDocument(); expect(screen.getAllByText('Unknown').length).toBeGreaterThan(4);
    expect(screen.queryByRole('button', { name: /backup|restart/i })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox', { name: /Concurrent transfers/ }), { target: { value: '3' } });
    expect(screen.getByText(/Restart required after this change/)).toBeInTheDocument();
    fireEvent.click(saveButton()); await screen.findByText(/A service restart would be required/);
    expect((await backend.jobs.snapshot()).jobs[0].state).toBe('running');
  });
  it('contains initial permission failures and ignores stale initial reads after navigation', async () => {
    const backend = createMockBackend(); let resolveOld!: (config: Configuration) => void;
    const original = await backend.configuration.get();
    const slow: BackendPorts = { ...backend, configuration: { ...backend.configuration, get: () => new Promise(resolve => { resolveOld = resolve; }) } };
    const result = render(<SettingsView ports={slow}/>); result.rerender(<SettingsView ports={backend}/>);
    await screen.findByRole('textbox', { name: 'Storage label' });
    await act(async () => { resolveOld({ ...original, driveLabels: { ...original.driveLabels, 'demo-files': 'Stale unwanted label' } }); });
    expect(labelInput()).toHaveValue('File Storage'); result.unmount();
    const denied: BackendPorts = { ...backend, configuration: { ...backend.configuration, get: async () => { throw domainError('PERMISSION_DENIED', 'SECRET_OWNER_ERROR'); } } };
    render(<SettingsView ports={denied}/>); expect(await screen.findByRole('alert')).toHaveTextContent('Owner access is required'); expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
  it('rejects whitespace labels, empty enabled endpoints, and invalid intervals', async () => {
    const backend = createMockBackend(); const config = await backend.configuration.get();
    expect(validateSettingsDraft({ ...config, driveLabels: { 'demo-files': '  ' }, health: { enabled: true, endpoint: '', intervalSeconds: 1 } })).toEqual(expect.objectContaining({ 'driveLabels.demo-files': expect.any(String), 'health.endpoint': expect.any(String), 'health.intervalSeconds': expect.any(String) }));
    expect(endpointHasPrivateParts('wss://demo.invalid?key=PRIVATE')).toBe(true);
  });
});
