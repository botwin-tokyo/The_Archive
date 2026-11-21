import {isPreviewImage} from '../../contracts/imagePreview';
import {createMockVideo} from './video';
import {createMockAnime} from './anime';
import {createMockMusic} from './music';
import type { z } from 'zod';
import { agentSummarySchema, configurationSchema, diagnosticSchema, directoryPageSchema, directoryQuerySchema, driveSchema, entryRefSchema, entrySchema, jobEventSchema, jobSchema, jobSnapshotSchema, locationSchema, serviceStatusSchema, syntheticSelectionSchema, trashEntrySchema } from '../../contracts/schemas';
import { domainError } from '../../contracts/errors';
import type { AgentSummary, BackendPorts, Configuration, DirectoryPage, Drive, Entry, Job, JobControl, JobState, ReadOptions } from '../../contracts';
import { BACKUPS_DRIVE_ID, createDrives, FILES_DRIVE_ID, FIXTURE_TIME } from '../../fixtures/drives';
import { createEntries, largeEntry } from '../../fixtures/entries';
import { createJobs, jobCapabilities } from '../../fixtures/jobs';
import { SCENARIOS, type MockScenario } from '../../fixtures/scenarios';
import { ScenarioClock } from './scenarioClock';
import { createOperationPort } from './operations';
import { immutable, type MockState } from './state';

export interface MockBackend extends BackendPorts {
  readonly mode: 'mock';
  readonly clock: ScenarioClock;
  readonly scenarios: readonly MockScenario[];
  readonly scenario: MockScenario;
  setScenario(scenario: MockScenario): void;
  reset(): void;
  dispose(): void;
  debug: { readonly subscriptionCount: number; readonly retainedRequestCount: number; setDriveState(id: string, state: Drive['state']): void; setBackendConnected(connected: boolean): void; emitDuplicate(): void; emitGap(): void; };
}
export interface MockBackendOptions { scenario?: MockScenario; seedJobs?: boolean; largeDirectoryCount?: number; }
function validate<T>(schema: z.ZodType<T>, input: unknown): T { const result = schema.safeParse(input); if (!result.success) throw domainError('INVALID_RESPONSE', 'The simulated response failed runtime validation', true); return immutable(result.data); }
function defaults(): Configuration { return { revision: 1, driveLabels: { [FILES_DRIVE_ID]: 'File Storage', [BACKUPS_DRIVE_ID]: 'Memory Harness Backups' }, transferConcurrency: 2, health: { enabled: false, endpoint: '', intervalSeconds: 30 }, appearance: { reducedMotion: false, compactRows: false }, restartRequired: false, simulated: true }; }
function initialAgents(): AgentSummary[] { return [ { id: 'agent-harness', label: 'Memory Harness', scopes: ['read', 'write'], allowedRoots: [{ driveId: BACKUPS_DRIVE_ID, path: '' }], createdAt: FIXTURE_TIME, lastUsedAt: FIXTURE_TIME, revoked: false, revision: 1, credentialDisplay: 'DEMO · nonfunctional placeholder' }, { id: 'agent-research', label: 'Research agent', scopes: ['read'], allowedRoots: [{ driveId: FILES_DRIVE_ID, path: 'Archive' }], createdAt: FIXTURE_TIME, lastUsedAt: null, revoked: false, revision: 1, credentialDisplay: 'DEMO · nonfunctional placeholder' } ]; }
function compareBytes(a: string | null, b: string | null): number { if (a === null || b === null) return a === b ? 0 : a === null ? 1 : -1; const left = BigInt(a); const right = BigInt(b); return left === right ? 0 : left < right ? -1 : 1; }

/** In-memory service simulation. No filesystem, sockets, credentials, intervals, or persistence. */
export function createMockBackend(options: MockBackendOptions = {}): MockBackend {
  const clock = new ScenarioClock();
  let disposed = false;
  let generation = 0;
  let reads = 0;
  let tickRemainder = 0;
  let lastEvent: ReturnType<typeof jobEventSchema.parse> | undefined;
  const state: MockState = { clock, scenario: options.scenario ?? 'normal', drives: [], entries: new Map(), jobs: new Map(), trash: new Map(), trashContents: new Map(), agents: [], configuration: defaults(), status: { schemaVersion: 1, backend: 'connected', freshnessAt: clock.now(), api: 'not-configured', mcp: 'not-configured', health: 'not-configured', authenticatedSessions: 0, stale: false, simulated: true }, diagnostics: [], directoryRevision: 1, cursor: 0, sequence: 0, requests: new Map(), plans: new Map(), eventListeners: new Set(), statusListeners: new Set(), emitJob: () => {}, commitJob: () => {}, validateJob: () => {}, log: () => {} };
  state.log = (message, code = 'DEMO_EVENT') => { state.diagnostics.push({ id: `diagnostic-${++state.sequence}`, at: clock.now(), level: code === 'DEMO_EVENT' ? 'info' : 'warning', code, message: message.slice(0, 500), correlationId: `mock-${state.sequence}`, simulated: true }); state.diagnostics = state.diagnostics.slice(-200); };
  const capabilitiesForJob = (job: Job): Job['capabilities'] => {
    if (job.parentId) return { pause: false, resume: false, cancel: false, retry: false };
    const capabilities = jobCapabilities(job.state);
    const agent = job.actor.kind === 'agent' ? state.agents.find((item) => item.id === job.actor.id) : undefined;
    if (job.actor.kind === 'agent' && (!agent || agent.revoked)) { capabilities.pause = false; capabilities.resume = false; capabilities.retry = false; }
    if (job.error && !job.error.retryable) capabilities.retry = false;
    return capabilities;
  };
  state.emitJob = (job) => {
    job.revision++; job.updatedAt = clock.now(); job.capabilities = capabilitiesForJob(job); state.jobs.set(job.id, job);
    const event = validate(jobEventSchema, { schemaVersion: 1, cursor: ++state.cursor, job });
    const prior = lastEvent; lastEvent = event;
    if (!(state.scenario === 'event-gap' && event.cursor % 3 === 1)) for (const listener of [...state.eventListeners]) { listener(event); if (state.scenario === 'duplicate-events') listener(event); if (state.scenario === 'out-of-order-events' && prior) listener(prior); }
    for (const childId of job.childIds) {
      const child = state.jobs.get(childId); if (!child) continue;
      child.state = job.state; child.error = job.error; child.outcome = job.outcome;
      child.bytesDone = child.bytesTotal !== null && job.bytesTotal !== null && BigInt(job.bytesTotal) > 0n ? String(BigInt(child.bytesTotal) * BigInt(job.bytesDone) / BigInt(job.bytesTotal)) : '0';
      child.rateBytesPerSecond = null; child.etaSeconds = job.etaSeconds;
      if (job.state === 'completed') {
        const applied = job.resultingNames.includes(child.label);
        child.outcome = job.outcome === 'partial' ? applied ? 'complete' : 'skipped' : job.outcome;
        child.resultingNames = applied ? [child.label] : [];
      }
      state.emitJob(child);
    }
  };
  const publishStatus = () => { state.status.freshnessAt = clock.now(); for (const listener of [...state.statusListeners]) listener(validate(serviceStatusSchema, state.status)); };
  const setDriveState = (id: string, value: Drive['state']) => { const drive = state.drives.find((item) => item.id === id); if (!drive) throw domainError('NOT_FOUND', 'The demo drive does not exist'); drive.state = value; drive.revision++; drive.capabilities = { browse: value !== 'offline' && value !== 'unconfigured', write: value === 'online', trash: value === 'online' || value === 'full', purge: value === 'online' || value === 'full' }; if (value === 'full') { drive.capacityBytes = '1073741824'; drive.freeBytes = '0'; } state.directoryRevision++; publishStatus(); };
  function reset(scenario: MockScenario): void {
    if (!SCENARIOS.includes(scenario)) throw domainError('INVALID_REQUEST', 'Unknown demo scenario');
    generation++; clock.reset(); tickRemainder = 0; reads = 0; lastEvent = undefined;
    state.scenario = scenario; state.drives = createDrives(); state.entries = new Map(createEntries().map((entry) => [entry.id, entry])); state.jobs = new Map((options.seedJobs === false ? [] : createJobs()).map((job) => [job.id, job])); state.trash.clear(); state.trashContents.clear(); state.requests.clear(); state.plans.clear(); state.agents = initialAgents(); state.configuration = defaults(); state.directoryRevision++; state.cursor = 0; state.sequence = 0; state.diagnostics = [];
    state.status = { schemaVersion: 1, backend: 'connected', freshnessAt: clock.now(), api: 'not-configured', mcp: 'not-configured', health: 'not-configured', authenticatedSessions: 0, stale: false, simulated: true };
    if (scenario === 'offline-drive') setDriveState(FILES_DRIVE_ID, 'offline');
    if (scenario === 'read-only-drive') setDriveState(FILES_DRIVE_ID, 'read-only');
    if (scenario === 'full-drive') setDriveState(BACKUPS_DRIVE_ID, 'full');
    if (scenario === 'conflict') { const entry = createEntries().find((item) => item.id === 'file-2')!; state.entries.set('fixture-conflict', { ...entry, id: 'fixture-conflict', path: `Incoming/${entry.name}`, parentPath: 'Incoming', sizeBytes: '3' }); }
    if (scenario === 'missing-parent') { const entry = [...state.entries.values()].find((item) => item.path === 'Archive/Documents/Archive inventory.csv')!; state.entries.delete(entry.id); const parent = [...state.entries.values()].find((item) => item.path === 'Archive/Documents'); if (parent) state.entries.delete(parent.id); state.trash.set(entry.id, { entry: { ...entry, revision: entry.revision + 1 }, originalPath: entry.path, deletedAt: clock.now(), deletedBy: { id: 'owner', label: 'Local owner', kind: 'owner' }, retainedBytes: entry.sizeBytes }); state.trashContents.set(entry.id, []); }
    if (scenario === 'revoked-agent') state.agents[0]!.revoked = true;
    if (scenario === 'lost-connection') { state.status.backend = 'unavailable'; state.status.stale = true; }
    if (scenario === 'telemetry-error') state.status.health = 'reconnecting';
    if (scenario === 'unknown-total') { const job = state.jobs.get('demo-job-harness'); if (job) { job.bytesTotal = null; job.etaSeconds = null; } }
    if (scenario === 'job-history') { const template = createJobs()[0]!; for (let i = 0; i < 1000; i++) { const job: Job = { ...template, id: `history-${i}`, requestId: `history-request-${i}`, state: 'completed', bytesDone: template.bytesTotal!, capabilities: jobCapabilities('completed'), outcome: 'complete' }; state.jobs.set(job.id, job); } }
    for (const job of state.jobs.values()) job.capabilities = capabilitiesForJob(job);
    state.log('Demo dataset initialized. No real storage, credentials, or services are connected.'); publishStatus();
  }
  const ensureConnected = () => { if (disposed) throw domainError('UNAVAILABLE', 'The demo adapter has been disposed'); if (state.status.backend !== 'connected') throw domainError('UNAVAILABLE', 'The simulated backend is unavailable. Cached data may be stale.', true, 'mock-connection', ['reconnect']); };
  const read = async (options?: ReadOptions) => { const atGeneration = generation; ensureConnected(); if (options?.signal?.aborted) throw domainError('ABORTED', 'This read was canceled'); const sequence = ++reads; const delay = state.scenario === 'slow-reads' ? 1000 : state.scenario === 'out-of-order-reads' ? sequence % 2 === 1 ? 1000 : 100 : 0; await clock.delay(delay, options?.signal); if (options?.signal?.aborted || atGeneration !== generation) throw domainError('ABORTED', 'This read belongs to an obsolete demo session'); ensureConnected(); };
  const requireDrive = (id: string) => { const drive = state.drives.find((item) => item.id === id); if (!drive) throw domainError('NOT_FOUND', 'This drive identity is not configured'); if (!drive.capabilities.browse) throw domainError('UNAVAILABLE', 'This demo drive is offline', true); return drive; };
  const decoratedEntry = (entry: Entry): Entry => { const drive = state.drives.find((item) => item.id === entry.driveId)!; if (drive.state === 'read-only') return { ...entry, capabilities: { ...entry.capabilities, move: false, rename: false, trash: false, reason: 'This demo drive is read only' } }; return entry; };
  const transition = (job: Job, value: JobState) => { job.state = value; if (value !== 'running') { job.rateBytesPerSecond = null; job.etaSeconds = null; } state.emitJob(job); };
  function tick(): void {
    if (state.status.backend !== 'connected') return;
    for (const job of [...state.jobs.values()]) {
      if (job.parentId) continue;
      const agent = state.agents.find((item) => item.id === job.actor.id);
      if (agent?.revoked && !['failed', 'completed', 'canceled'].includes(job.state)) { job.error = domainError('PERMISSION_REVOKED', 'This simulated agent was revoked', false).detail; transition(job, 'failed'); continue; }
      const plan = state.plans.get(job.id);
      const involvedDriveIds = new Set([job.source?.driveId, job.destination?.driveId, ...(plan?.targets ?? []).map((target) => target.driveId)].filter((id): id is string => Boolean(id)));
      const unavailableDrive = [...involvedDriveIds].some((id) => { const drive = state.drives.find((item) => item.id === id); return !drive || drive.state === 'offline' || drive.state === 'unconfigured'; });
      if (unavailableDrive && ['queued', 'preparing', 'running', 'verifying', 'committing'].includes(job.state)) { transition(job, 'waiting-for-drive'); continue; }
      if (job.state === 'queued') { const active = [...state.jobs.values()].filter((item) => !item.parentId && ['preparing', 'running', 'verifying', 'committing'].includes(item.state)).length; if (active < state.configuration.transferConcurrency) transition(job, 'preparing'); }
      else if (job.state === 'preparing') { try { state.validateJob(job); transition(job, 'running'); } catch (error) { job.error = error && typeof error === 'object' && 'detail' in error ? (error as { detail: Job['error'] }).detail : domainError('INTERRUPTED', 'The simulated operation could not be revalidated', true).detail; transition(job, 'failed'); } }
      else if (job.state === 'running') {
        if (job.bytesTotal === null) continue;
        const total = BigInt(job.bytesTotal); const amount = total / 8n + 1n; const done = BigInt(job.bytesDone) + amount;
        job.bytesDone = String(done > total ? total : done); job.rateBytesPerSecond = String(amount * 2n); job.etaSeconds = Number((total - BigInt(job.bytesDone)) / amount) / 2;
        if (BigInt(job.bytesDone) === total) transition(job, 'verifying'); else state.emitJob(job);
      } else if (job.state === 'verifying') {
        if (state.scenario === 'integrity-failure') { job.error = domainError('INTEGRITY_FAILED', 'Simulated integrity verification failed; destination was not published', true, `mock-${job.id}`, ['retry', 'discard']).detail; transition(job, 'failed'); } else transition(job, 'committing');
      } else if (job.state === 'committing') {
        try { state.commitJob(job); if (job.state === 'committing') { job.outcome ??= 'complete'; transition(job, 'completed'); } } catch (error) { job.error = error && typeof error === 'object' && 'detail' in error ? (error as { detail: Job['error'] }).detail : domainError('INTERRUPTED', 'The simulated operation could not be committed', true).detail; transition(job, 'failed'); }
      } else if (job.state === 'pausing') transition(job, 'paused');
      else if (job.state === 'canceling') transition(job, 'canceled');
    }
  }
  const unsubscribeClock = clock.subscribe((milliseconds) => { tickRemainder += milliseconds; while (tickRemainder >= 500) { tickRemainder -= 500; tick(); } });
  reset(state.scenario);
  const backend: MockBackend = {
    mode: 'mock', music: createMockMusic(), video:createMockVideo(), anime:createMockAnime(), clock, scenarios: SCENARIOS, get scenario() { return state.scenario; }, setScenario: reset, reset: () => reset('normal'), dispose: () => { disposed = true; unsubscribeClock(); clock.dispose(); state.eventListeners.clear(); state.statusListeners.clear(); },
    drives: { list: async (options) => { await read(options); return state.drives.map((drive) => validate(driveSchema, drive)); } },
    files: {
      previewImage:async(ref,options)=>{
        const entry=await backend.files.stat(ref,options);
        if(entry.kind!=='file'||!entry.capabilities.read||!isPreviewImage(entry.name))throw domainError('UNSUPPORTED','Select a PNG, JPEG, GIF, or WebP image.');
        return {mime:'image/png',width:96,height:64,sizeBytes:'190',dataUrl:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABACAIAAABqVuVZAAAAhUlEQVR4nO3QQQmAQAAAwYsh+DKrgaxjHBvoS9zDgQ2wzFi2VTeNzw/iAQIECBCgcIAAAQIEKBygF4D285g3QIAAAQIUDhAgQIAAhQMECBAgQOEAAQIECFA4QIAAAQIUDhAgQIAAhQMECBAgQOEAAQI0PdCvAgQIECBA4QABAgQIUDhAD100Xk0vDmVS9gAAAABJRU5ErkJggg=='};
      },
      list: async (input, options) => {
        const parsed = directoryQuerySchema.safeParse(input); if (!parsed.success) throw domainError('INVALID_REQUEST', 'The directory query is invalid'); const query = parsed.data; await read(options); requireDrive(query.location.driveId);
        const { driveId, path } = query.location;
        if (path !== '' && ![...state.entries.values()].some((entry) => entry.driveId === driveId && entry.path === path && entry.kind === 'directory')) throw domainError('NOT_FOUND', 'The selected directory no longer exists', true, query.requestId, ['refresh']);
        const limit = query.limit ?? 80; const signature = JSON.stringify([driveId, path, query.sort ?? 'name', query.direction ?? 'asc', query.search ?? '', query.scope ?? 'directory', query.foldersFirst ?? true, limit]);
        let offset = 0;
        if (query.cursor) { let cursor: unknown; try { cursor = JSON.parse(query.cursor); } catch { throw domainError('INVALID_REQUEST', 'The directory cursor is invalid'); } if (!cursor || typeof cursor !== 'object' || Array.isArray(cursor)) throw domainError('INVALID_REQUEST', 'The directory cursor is invalid'); const parsedCursor = cursor as { revision?: unknown; signature?: unknown; offset?: unknown }; if (parsedCursor.revision !== state.directoryRevision) throw domainError('STALE_REVISION', 'The directory changed; refresh before continuing', true); if (parsedCursor.signature !== signature || typeof parsedCursor.offset !== 'number' || !Number.isSafeInteger(parsedCursor.offset) || parsedCursor.offset < 0) throw domainError('INVALID_REQUEST', 'The cursor belongs to a different query'); offset = parsedCursor.offset; }
        if (state.scenario === 'malformed-listing') return validate(directoryPageSchema, { schemaVersion: 99 });
        let entries: Entry[] = [];
        let total = 0;
        const largeCount = optionsLargeCount();
        if (driveId === FILES_DRIVE_ID && path === 'Large directory') {
          const indexes: number[] = [];
          for (let i = 0; i < largeCount; i++) if (!query.search || `Synthetic item ${String(i + 1).padStart(6, '0')}.bin`.toLowerCase().includes(query.search.toLowerCase())) indexes.push(i);
          if (query.direction === 'desc') indexes.reverse(); total = indexes.length; entries = indexes.slice(offset, offset + limit).map(largeEntry);
        } else {
          const search = query.search?.toLocaleLowerCase() ?? '';
          entries = state.scenario === 'empty' ? [] : [...state.entries.values()].filter((entry) => entry.driveId === driveId && (query.scope === 'recursive' ? entry.path !== path && (path === '' || entry.path.startsWith(`${path}/`)) : entry.parentPath === path) && (!search || entry.name.toLocaleLowerCase().includes(search)));
          entries.sort((a, b) => { if (query.foldersFirst !== false && (a.kind === 'directory') !== (b.kind === 'directory')) return a.kind === 'directory' ? -1 : 1; const value = query.sort === 'size' ? compareBytes(a.sizeBytes, b.sizeBytes) : query.sort === 'modified' ? (a.modifiedAt ?? '').localeCompare(b.modifiedAt ?? '') : query.sort === 'type' ? a.type.localeCompare(b.type) : a.name.localeCompare(b.name, 'en', { numeric: true, sensitivity: 'base' }); return (query.direction === 'desc' ? -value : value) || a.id.localeCompare(b.id); }); total = entries.length; entries = entries.slice(offset, offset + limit);
        }
        const page: DirectoryPage = { schemaVersion: 1, requestId: query.requestId, location: query.location, revision: state.directoryRevision, entries: entries.map(decoratedEntry), nextCursor: offset + limit < total ? JSON.stringify({ revision: state.directoryRevision, signature, offset: offset + limit }) : null, total, stale: false }; return validate(directoryPageSchema, page);
      },
      stat: async (input, options) => { const ref = entryRefSchema.parse(input); await read(options); requireDrive(ref.driveId); const entry = state.entries.get(ref.id) ?? (ref.id.startsWith('large-') && /^large-\d+$/.test(ref.id) && Number(ref.id.slice(6)) < optionsLargeCount() ? largeEntry(Number(ref.id.slice(6))) : undefined); if (!entry || entry.driveId !== ref.driveId || entry.path !== ref.path) throw domainError('NOT_FOUND', 'This item no longer exists', true); if (entry.revision !== ref.revision || state.scenario === 'stale-revision') throw domainError('STALE_REVISION', 'This item changed since it was selected', true, 'mock-metadata', ['refresh']); return validate(entrySchema, decoratedEntry(entry)); },
    },
    operations: createOperationPort(state),
    jobs: {
      snapshot: async (options) => { await read(options); return validate(jobSnapshotSchema, { schemaVersion: 1, cursor: state.cursor, jobs: [...state.jobs.values()], stale: false }); },
      subscribe: (listener) => { state.eventListeners.add(listener); return () => state.eventListeners.delete(listener); },
      control: async (id, action: JobControl) => { ensureConnected(); const job = state.jobs.get(id); if (!job) throw domainError('NOT_FOUND', 'This demo job does not exist'); const agent = job.actor.kind === 'agent' ? state.agents.find((item) => item.id === job.actor.id) : undefined; if (action !== 'cancel' && job.actor.kind === 'agent' && (!agent || agent.revoked)) throw domainError(agent?.revoked ? 'PERMISSION_REVOKED' : 'PERMISSION_DENIED', 'This simulated identity cannot resume or retry work'); job.capabilities = capabilitiesForJob(job); if (!job.capabilities[action]) throw domainError('UNSUPPORTED', `This job cannot ${action} in its current state`); if (action === 'retry') { const next: Job = { ...structuredClone(job), id: `job-${++state.sequence}`, requestId: `retry-${state.sequence}`, revision: 0, state: 'queued', bytesDone: '0', previousAttemptId: job.id, error: null, outcome: null, resultingNames: [], createdAt: clock.now() }; const plan = state.plans.get(job.id); if (plan) state.plans.set(next.id, plan); next.childIds = []; for (const oldId of job.childIds) { const oldChild = state.jobs.get(oldId); if (!oldChild) continue; const childId = `job-${++state.sequence}`; const child: Job = { ...structuredClone(oldChild), id: childId, requestId: `${next.requestId}:item:${next.childIds.length}`, parentId: next.id, state: 'queued', revision: 0, bytesDone: '0', previousAttemptId: oldChild.id, error: null, outcome: null, resultingNames: [], createdAt: clock.now() }; next.childIds.push(childId); state.jobs.set(childId, child); } state.emitJob(next); return validate(jobSchema, next); } transition(job, action === 'pause' ? 'pausing' : action === 'resume' ? 'preparing' : 'canceling'); return validate(jobSchema, job); },
    },
    trash: { list: async (driveId, options) => { await read(options); requireDrive(driveId); return [...state.trash.values()].filter((item) => item.entry.driveId === driveId).map((item) => validate(trashEntrySchema, item)); } },
    configuration: {
      get: async (options) => { await read(options); return validate(configurationSchema, state.configuration); },
      save: async (draft) => { ensureConnected(); const parsed = configurationSchema.safeParse(draft); if (!parsed.success) throw domainError('INVALID_REQUEST', 'Review the configuration fields. Only secure synthetic endpoint syntax is accepted.'); if (draft.revision !== state.configuration.revision) throw domainError('STALE_REVISION', 'Configuration changed; reload the saved version', true); if (state.scenario === 'settings-timeout') throw domainError('TIMEOUT', 'Simulated settings save timed out. The draft was not saved.', true); if (state.scenario === 'settings-rejected') throw domainError('PERMISSION_DENIED', 'Simulated owner permission denied'); state.configuration = { ...parsed.data, revision: draft.revision + 1, restartRequired: draft.transferConcurrency !== state.configuration.transferConcurrency }; for (const drive of state.drives) drive.label = state.configuration.driveLabels[drive.id] ?? drive.label; state.status.health = !draft.health.enabled ? draft.health.endpoint ? 'disabled' : 'not-configured' : 'simulated-connected'; publishStatus(); state.log('Synthetic configuration updated. No connection or restart was performed.'); return validate(configurationSchema, state.configuration); },
      testHealth: async () => ({ simulated: true, success: Boolean(state.configuration.health.endpoint) && state.scenario !== 'telemetry-error', message: state.configuration.health.endpoint ? state.scenario === 'telemetry-error' ? 'Simulated connection failed. Storage remains available.' : 'Simulated connection test passed. No network connection was made.' : 'No endpoint configured. No network connection was made.' }),
    },
    agents: {
      list: async (options) => { await read(options); return state.agents.map((agent) => validate(agentSummarySchema, agent)); },
      updateScopes: async (id, revision, scopes, allowedRoots) => { ensureConnected(); const agent = state.agents.find((item) => item.id === id); if (!agent) throw domainError('NOT_FOUND', 'This demo agent does not exist'); if (agent.revision !== revision) throw domainError('STALE_REVISION', 'The simulated agent changed', true); const changed = agentSummarySchema.safeParse({ ...agent, scopes, allowedRoots: allowedRoots.map((root) => locationSchema.parse(root)), revision: revision + 1 }); if (!changed.success) throw domainError('INVALID_REQUEST', 'The simulated scopes are invalid'); Object.assign(agent, changed.data); state.log('Simulated agent scopes updated. No real permissions were changed.'); return validate(agentSummarySchema, agent); },
      revoke: async (id, revision) => { ensureConnected(); const agent = state.agents.find((item) => item.id === id); if (!agent) throw domainError('NOT_FOUND', 'This demo agent does not exist'); if (agent.revision !== revision) throw domainError('STALE_REVISION', 'The simulated agent changed', true); agent.revoked = true; agent.revision++; for (const job of state.jobs.values()) if (job.actor.id === id && !job.parentId) state.emitJob(job); state.log('A demo agent was revoked. No real credential exists.'); return validate(agentSummarySchema, agent); },
    },
    diagnostics: { list: async (options) => { if (options?.signal?.aborted) throw domainError('ABORTED', 'This read was canceled'); return state.diagnostics.map((item) => validate(diagnosticSchema, item)); } },
    selection: { select: async (kind) => { if (state.scenario === 'picker-cancel') return null; return validate(syntheticSelectionSchema, { handle: `mock:${kind}:${++state.sequence}`, label: kind === 'folder' ? 'Synthetic upload folder' : kind === 'files' ? 'Synthetic file selection' : 'Synthetic download destination', kind, items: kind === 'download-destination' ? [] : kind === 'folder' ? [{ relativePath: 'Demo collection/notes.txt', sizeBytes: '4096' }, { relativePath: 'Demo collection/nested/sample.bin', sizeBytes: '5242880' }] : [{ relativePath: 'Selected demo file.bin', sizeBytes: '8388608' }], simulated: true }); } },
    status: { get: async (options) => { if (options?.signal?.aborted) throw domainError('ABORTED', 'This read was canceled'); return validate(serviceStatusSchema, state.status); }, subscribe: (listener) => { state.statusListeners.add(listener); return () => state.statusListeners.delete(listener); } },
    debug: { get subscriptionCount() { return state.eventListeners.size + state.statusListeners.size; }, get retainedRequestCount() { return state.requests.size; }, setDriveState, setBackendConnected: (connected) => { state.status.backend = connected ? 'connected' : 'unavailable'; state.status.stale = !connected; publishStatus(); }, emitDuplicate: () => { if (lastEvent) for (const listener of state.eventListeners) listener(lastEvent); }, emitGap: () => { state.cursor++; const job = [...state.jobs.values()][0]; if (job) state.emitJob(job); } },
  };
  function optionsLargeCount(): number { return Math.max(0, Math.min(options.largeDirectoryCount ?? 100000, 100000)); }
  return backend;
}
