import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { lazy, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createMockBackend } from '../../src/adapters/mock';
import type { DirectoryQuery, Entry, JobPort } from '../../src/contracts';
import { domainError, refOf } from '../../src/contracts';
import { AppShell } from '../../src/shell';
import App from '../../src/app/App';
import { FeatureBoundary as RootRecoveryBoundary } from '../../src/app/AppErrorBoundary';
import * as dependencies from '../../src/app/createDependencies';
import { FilesView } from '../../src/features/files';
import { Inspector } from '../../src/features/inspector';
import { TransferQueue } from '../../src/features/transfers';
import { createJobStore } from '../../src/features/transfers/jobStore';
import { SettingsView } from '../../src/features/settings';
import { OperationDialog } from '../../src/features/operations';
import { BACKUPS_DRIVE_ID, FILES_DRIVE_ID, INITIAL_LOCATION } from '../../src/fixtures/drives';

beforeEach(() => {
  localStorage.clear();
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const shell = (main: ReactNode, queue: ReactNode = <div>Independent queue</div>, inspector: ReactNode = <div>Independent inspector</div>, route = 'files') => <AppShell route={route} onRoute={vi.fn()} sidebar={<div>Both drive identities</div>} queue={queue} inspector={inspector}>{main}</AppShell>;
function Crash({ crash, children }: { crash: boolean; children: ReactNode }) { if (crash) throw Error('Injected local render fault'); return children; }

describe('composed fault acceptance matrix', () => {

  it('explains synthetic session reset and the unproven service boundary after a root render failure', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    render(<RootRecoveryBoundary name="Application"><Crash crash><p>Unreachable interface</p></Crash></RootRecoveryBoundary>);
    const fallback = screen.getByRole('alert');
    expect(within(fallback).getByRole('heading', { name: 'The Archive needs to restart' })).toBeTruthy();
    expect(fallback.textContent).toContain('Restarting resets this synthetic session and its in-memory jobs');
    expect(fallback.textContent).toContain('No real storage or independent backend is connected');
    expect(fallback.textContent).toContain('does not establish future service survival');
    expect(within(fallback).getByRole('button', { name: 'Restart demo interface' })).toBeTruthy();
    expect(fallback.textContent).not.toContain('Other work is unaffected');
    expect(screen.queryByText('Unreachable interface')).toBeNull();
    // No window reload or actual renderer process is claimed by this DOM test.
  });

  it.each(['files', 'queue', 'inspector'] as const)('isolates a %s render crash and retries only its boundary', async target => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const backend = createMockBackend(); const store = createJobStore(backend.jobs); store.start(); await store.reconcile();
    let crash = false;
    const compose = () => shell(<Crash crash={crash && target === 'files'}><button>Retained Files action</button></Crash>, <Crash crash={crash && target === 'queue'}><TransferQueue store={store}/></Crash>, <Crash crash={crash && target === 'inspector'}><button>Retained inspector action</button></Crash>);
    const view = render(compose());
    const before = store.getSnapshot().jobs.map(job => job.id);
    crash = true; view.rerender(compose());
    expect(screen.getByRole('alert').textContent).toContain('couldn’t load');
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeTruthy();
    expect(screen.getByText('Both drive identities')).toBeTruthy();
    if (target !== 'files') expect(screen.getByRole('button', { name: 'Retained Files action' })).toBeTruthy();
    if (target !== 'queue') expect(screen.getByRole('list', { name: 'Transfer jobs' })).toBeTruthy();
    if (target !== 'inspector') expect(screen.getByRole('button', { name: 'Retained inspector action' })).toBeTruthy();
    await act(async () => { backend.clock.advance(500); });
    expect(store.getSnapshot().cursor).toBeGreaterThan(0);
    expect(store.getSnapshot().jobs.map(job => job.id).sort()).toEqual([...before].sort());
    crash = false; view.rerender(compose()); fireEvent.click(screen.getByRole('button', { name: 'Retry panel' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByRole('button', { name: 'Retained Files action' })).toBeTruthy();
    expect(screen.getByRole('list', { name: 'Transfer jobs' })).toBeTruthy();
    expect(backend.debug.subscriptionCount).toBe(1); view.unmount(); store.stop(); backend.dispose();
  });

  it('contains a rejected supporting-route import and allows navigation back to Files', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const BrokenRoute = lazy(async () => { throw Error('Injected unavailable chunk'); });
    const view = render(shell(<BrokenRoute/>, undefined, undefined, 'diagnostics'));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText('Independent queue')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Files' })).toBeTruthy();
    view.rerender(shell(<button>Recovered Files action</button>));
    expect(await screen.findByRole('button', { name: 'Recovered Files action' })).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps owner, Harness and other-agent jobs plus the second drive available after one drive goes offline', async () => {
    const backend = createMockBackend(); const store = createJobStore(backend.jobs); store.start(); await store.reconcile();
    render(shell(<FilesView port={backend.files} location={{ driveId: BACKUPS_DRIVE_ID, path: 'Snapshots' }} onNavigate={vi.fn()} onSelection={vi.fn()}/>, <TransferQueue store={store}/>));
    await screen.findByText('harness-demo-001.tar');
    await act(async () => { backend.debug.setDriveState(FILES_DRIVE_ID, 'offline'); backend.clock.advance(500); });
    const jobs = store.getSnapshot().jobs;
    expect(new Set(jobs.map(job => job.actor.id)).size).toBe(3);
    expect(jobs.find(job => job.id === 'demo-job-harness')?.state).toBe('running');
    expect(jobs.find(job => job.id === 'demo-job-copy')?.state).toBe('waiting-for-drive');
    expect((await backend.drives.list()).map(drive => drive.id)).toEqual([FILES_DRIVE_ID, BACKUPS_DRIVE_ID]);
    expect(screen.getByText('harness-demo-001.tar')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Settings' })).toBeTruthy();
    await expect(backend.files.list({ location: INITIAL_LOCATION, requestId: 'offline-first-drive' })).rejects.toMatchObject({ detail: { code: 'UNAVAILABLE' } });
    store.stop(); backend.dispose();
  });

  it('contains malformed directory responses while the queue stays operable', async () => {
    const backend = createMockBackend({ scenario: 'malformed-listing' }); const store = createJobStore(backend.jobs); await store.reconcile();
    render(shell(<FilesView port={backend.files} location={INITIAL_LOCATION} onNavigate={vi.fn()} onSelection={vi.fn()}/>, <TransferQueue store={store}/>));
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Could not load this folder'));
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Pause harness-demo-002.tar' })).toBeTruthy();
    expect(screen.queryByText('Loading folder…')).toBeNull(); backend.dispose();
  });

  it('retains table and queue after asynchronous metadata failure', async () => {
    const backend = createMockBackend(); const page = await backend.files.list({ location: INITIAL_LOCATION, requestId: 'metadata-fault' }); const selected = page.entries[0]; const store = createJobStore(backend.jobs); await store.reconcile();
    render(shell(<FilesView port={backend.files} location={INITIAL_LOCATION} onNavigate={vi.fn()} onSelection={vi.fn()}/>, <TransferQueue store={store}/>, <Inspector port={{ ...backend.files, stat: async () => { throw domainError('TIMEOUT', 'Synthetic metadata timeout'); } }} selection={[selected]} location={INITIAL_LOCATION} onOperation={vi.fn()}/>));
    await screen.findByRole('button', { name: 'Retry metadata' });
    expect(await screen.findByRole('table', { name: 'Files in current folder' })).toBeTruthy();
    expect(screen.getByRole('list', { name: 'Transfer jobs' })).toBeTruthy(); backend.dispose();
  });

  it('preserves a settings draft after timeout and refuses duplicate submissions until reconciliation', async () => {
    const backend = createMockBackend({ scenario: 'settings-timeout' }); const save = vi.fn(backend.configuration.save);
    const ports = { ...backend, configuration: { ...backend.configuration, save } };
    render(shell(<SettingsView ports={ports}/>, undefined, null, 'settings'));
    const input = await screen.findByRole('textbox', { name: 'Storage label' }); fireEvent.change(input, { target: { value: 'Unsaved test label' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save demo settings' }));
    await screen.findByText(/outcome is uncertain/i);
    expect((input as HTMLInputElement).value).toBe('Unsaved test label');
    const saveButton = screen.getByRole('button', { name: 'Save demo settings' }); expect((saveButton as HTMLButtonElement).disabled).toBe(true); fireEvent.click(saveButton); expect(save).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Independent queue')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Check saved state' })); await screen.findByText(/your changes were not saved/i);
    expect((input as HTMLInputElement).value).toBe('Unsaved test label'); expect(save).toHaveBeenCalledTimes(1); backend.dispose();
  });

  it('marks cached entries stale after a rejected refresh and disables mutations', async () => {
    const backend = createMockBackend(); let disconnected = false;
    const port = { ...backend.files, list: async (query: DirectoryQuery) => { if (disconnected) throw domainError('UNAVAILABLE', 'Synthetic backend disconnected'); return backend.files.list(query); } };
    const onOperation = vi.fn(); const props = { port, location: INITIAL_LOCATION, onNavigate: vi.fn(), onSelection: vi.fn(), onOperation };
    const view = render(<FilesView {...props}/>); const selected = await screen.findByText('Field recordings.wav'); fireEvent.click(selected);
    expect((screen.getByRole('button', { name: 'Rename' }) as HTMLButtonElement).disabled).toBe(false);
    disconnected = true; view.rerender(<FilesView {...props} refreshKey={1}/>); await screen.findByText(/Showing cached entries/);
    expect(screen.getByText('Field recordings.wav')).toBeTruthy();
    for (const name of ['Upload', 'New folder', 'Rename', 'Move to Trash']) expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(selected.closest('tr')!, { key: 'Delete' }); expect(onOperation).not.toHaveBeenCalled(); backend.dispose();
  });

  it('retains cached queue jobs and disables controls when the snapshot disconnects', async () => {
    const backend = createMockBackend(); let disconnected = false;
    const port: JobPort = { ...backend.jobs, snapshot: async () => { if (disconnected) throw domainError('UNAVAILABLE', 'Synthetic disconnect'); return backend.jobs.snapshot(); } };
    const store = createJobStore(port); await store.reconcile(); render(<TransferQueue store={store}/>);
    disconnected = true; await act(async () => { await store.reconcile(); });
    expect(screen.getByRole('alert').textContent).toContain('Existing jobs are retained');
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect((screen.getByRole('button', { name: 'Pause harness-demo-002.tar' }) as HTMLButtonElement).disabled).toBe(true); backend.dispose();
  });


  it.each(['FULL_DISK', 'INTEGRITY_FAILED'] as const)('isolates a single %s job from active work and browsing', async code => {
    const backend = createMockBackend(); const snapshot = await backend.jobs.snapshot();
    const jobs = snapshot.jobs.map(job => job.id === 'demo-job-copy' ? { ...job, state: 'failed' as const, error: domainError(code, 'Injected failure of this job only', true).detail, capabilities: { pause: false, resume: false, cancel: false, retry: true } } : job);
    const store = createJobStore({ ...backend.jobs, snapshot: async () => ({ ...snapshot, jobs }) }); await store.reconcile();
    render(shell(<FilesView port={backend.files} location={INITIAL_LOCATION} onNavigate={vi.fn()} onSelection={vi.fn()}/>, <TransferQueue store={store}/>));
    expect(await screen.findByText('Project notes.md')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Pause harness-demo-002.tar' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resume Field recordings.wav' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry Reference collection' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Details for Reference collection' }));
    expect(screen.getByText(`${code}: Injected failure of this job only`)).toBeTruthy(); backend.dispose();
  });

  it('keeps telemetry unavailable independently of storage and job progress', async () => {
    const backend = createMockBackend({ scenario: 'telemetry-error' });
    const first = await backend.jobs.snapshot(); backend.clock.advance(500); const later = await backend.jobs.snapshot();
    expect((await backend.status.get())).toMatchObject({ backend: 'connected', health: 'reconnecting', stale: false });
    expect((await backend.files.list({ location: INITIAL_LOCATION, requestId: 'telemetry-independent' })).entries.length).toBeGreaterThan(0);
    expect(later.cursor).toBeGreaterThan(first.cursor); backend.dispose();
  });


  it('disables retry for revoked-agent work while owner and other identities continue', async () => {
    const backend = createMockBackend(); const store = createJobStore(backend.jobs); store.start(); await store.reconcile();
    render(<TransferQueue store={store}/>);
    const agent = (await backend.agents.list()).find(item => item.id === 'agent-harness')!;
    await act(async () => { await backend.agents.revoke(agent.id, agent.revision); backend.clock.advance(500); });
    const jobs = store.getSnapshot().jobs;
    expect(jobs.find(job => job.actor.id === agent.id)).toMatchObject({ state: 'failed', error: { code: 'PERMISSION_REVOKED' }, capabilities: { retry: false, resume: false, pause: false } });
    expect(screen.queryByRole('button', { name: 'Retry harness-demo-002.tar' })).toBeNull();
    await expect(backend.jobs.control('demo-job-harness', 'retry')).rejects.toMatchObject({ detail: { code: 'PERMISSION_REVOKED' } });
    await expect(backend.jobs.control('demo-job-harness', 'resume')).rejects.toMatchObject({ detail: { code: 'PERMISSION_REVOKED' } });
    expect(jobs.find(job => job.actor.id === 'owner')?.state).toBe('preparing');
    expect(jobs.find(job => job.actor.id === 'agent-research')?.state).toBe('paused');
    store.stop(); backend.dispose();
  });


  it('propagates a live App disconnect to cached UI and mutation controls, then reconciles on reconnect', async () => {
    const backend = createMockBackend(); vi.spyOn(dependencies, 'createDependencies').mockReturnValue(backend);
    const view = render(<App/>);
    const file = await screen.findByText('Project notes.md'); fireEvent.click(file);
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Rename' }).every(button => !(button as HTMLButtonElement).disabled)).toBe(true));
    await act(async () => { backend.debug.setBackendConnected(false); });
    expect(await screen.findByText('Cached demo data · backend unavailable. Mutations disabled.')).toBeTruthy();
    expect(screen.getByText('Demo backend: Unavailable')).toBeTruthy();
    expect(screen.getByRole('table', { name: 'Files in current folder' })).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Upload' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByRole('button', { name: 'Rename' }).every(button => (button as HTMLButtonElement).disabled)).toBe(true);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Pause harness-demo-002.tar' }) as HTMLButtonElement).disabled).toBe(true));
    await act(async () => { backend.debug.setBackendConnected(true); });
    await waitFor(() => expect(screen.queryByText('Cached demo data · backend unavailable. Mutations disabled.')).toBeNull());
    expect((screen.getByRole('button', { name: 'Upload' }) as HTMLButtonElement).disabled).toBe(false);
    await waitFor(() => expect((screen.getByRole('button', { name: 'Pause harness-demo-002.tar' }) as HTMLButtonElement).disabled).toBe(false));
    view.unmount(); expect(backend.debug.subscriptionCount).toBe(0); backend.dispose();
  }, 20_000);

  it('renders independent telemetry reconnect status in the integrated App without disabling storage', async () => {
    const backend = createMockBackend({ scenario: 'telemetry-error' }); vi.spyOn(dependencies, 'createDependencies').mockReturnValue(backend);
    const view = render(<App/>);
    await screen.findByRole('button', { name: 'Health WS: Reconnecting' }); await screen.findByText('Project notes.md');
    expect((screen.getByRole('button', { name: 'Upload' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText('Demo backend')).toBeTruthy();
    expect(screen.queryByText(/Mutations disabled/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Pause harness-demo-002.tar' })).toBeTruthy();
    view.unmount(); expect(backend.debug.subscriptionCount).toBe(0); backend.dispose();
  }, 20_000);

  it('rejects an operation when its drive becomes read-only while the review dialog is open', async () => {
    const backend = createMockBackend(); const page = await backend.files.list({ location: INITIAL_LOCATION, requestId: 'dialog-drive-change' }); const target = page.entries.find(entry => entry.kind === 'file') as Entry;
    const drives = await backend.drives.list(); const onAccepted = vi.fn(); const submit = vi.fn(backend.operations.submit);
    render(<OperationDialog draft={{ kind: 'rename', entries: [target], location: INITIAL_LOCATION }} ports={{ ...backend, operations: { ...backend.operations, submit } }} drives={drives} onClose={vi.fn()} onAccepted={onAccepted}/>);
    fireEvent.change(screen.getByRole('textbox', { name: 'Name' }), { target: { value: 'reviewed-name.bin' } }); backend.debug.setDriveState(FILES_DRIVE_ID, 'read-only');
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Rename' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('READ_ONLY'));
    expect(onAccepted).not.toHaveBeenCalled(); expect(submit).toHaveBeenCalledTimes(1);
    expect((await backend.files.stat(refOf(target))).name).toBe(target.name); backend.dispose();
  });
});
