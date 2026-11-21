import { domainError, refOf, safeError } from '../../contracts';
import { operationReceiptSchema, operationRequestSchema } from '../../contracts/schemas';
import type { Actor, Entry, EntryRef, Job, Location, OperationConflict, OperationPort, OperationReceipt, OperationRequest, TrashEntry } from '../../contracts';
import { createEntry } from '../../fixtures/entries';
import { jobCapabilities, OWNER_ACTOR } from '../../fixtures/jobs';
import { immutable, type MockState } from './state';

const join = (parent: string, name: string) => parent && name ? `${parent}/${name}` : parent || name;
const within = (path: string, root: string) => root === '' || path === root || path.startsWith(`${root}/`);
const parentOf = (path: string) => path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
const targetKey = (ref: EntryRef) => `${ref.driveId}:${ref.id}:${ref.path}:${ref.revision}`;
function actorFor(state: MockState, request: OperationRequest): Actor {
  if (!request.actorId || request.actorId === 'owner') return OWNER_ACTOR;
  const agent = state.agents.find((item) => item.id === request.actorId);
  if (!agent) throw domainError('PERMISSION_DENIED', 'This simulated identity is not registered');
  if (agent.revoked) throw domainError('PERMISSION_REVOKED', 'This simulated identity was revoked');
  return { id: agent.id, label: `${agent.label} · demo`, kind: 'agent' };
}
function requireScope(state: MockState, request: OperationRequest, location: Location, scope: 'read' | 'write' | 'trash' | 'purge'): void {
  const actor = actorFor(state, request);
  if (actor.kind === 'owner') return;
  const agent = state.agents.find((item) => item.id === actor.id)!;
  if (!agent.scopes.includes(scope) || !agent.allowedRoots.some((root) => root.driveId === location.driveId && within(location.path, root.path))) throw domainError('PERMISSION_DENIED', 'This simulated identity does not have permission for the selected location');
}
function requireDrive(state: MockState, id: string, write: boolean): void {
  const drive = state.drives.find((item) => item.id === id);
  if (!drive || drive.state === 'offline' || drive.state === 'unconfigured') throw domainError('UNAVAILABLE', 'The selected demo drive is unavailable', true, 'mock-drive', ['choose-destination', 'retry']);
  if (write && drive.state === 'read-only') throw domainError('READ_ONLY', 'The selected demo drive is read only');
}
function requireParent(state: MockState, location: Location): void {
  if (location.path && ![...state.entries.values()].some((entry) => entry.driveId === location.driveId && entry.path === location.path && entry.kind === 'directory')) throw domainError('NOT_FOUND', 'The destination parent no longer exists. Choose another destination or explicitly recreate its parents.', true, 'mock-parent', ['choose-destination']);
}
function targetEntry(state: MockState, ref: EntryRef, trash = false): Entry {
  const entry = trash ? state.trash.get(ref.id)?.entry : state.entries.get(ref.id);
  if (!entry || entry.driveId !== ref.driveId || entry.path !== ref.path) throw domainError('NOT_FOUND', 'A selected item no longer exists', true, 'mock-target', ['refresh']);
  if (entry.revision !== ref.revision || state.scenario === 'stale-revision') throw domainError('STALE_REVISION', 'A selected item changed. Refresh and confirm the current version.', true, 'mock-target', ['refresh']);
  return entry;
}
function descendants(state: MockState, entry: Entry): Entry[] { return [...state.entries.values()].filter((item) => item.driveId === entry.driveId && item.path.startsWith(`${entry.path}/`)); }
function collision(state: MockState, destination: Location, name: string, ignoredId?: string): Entry | undefined { return [...state.entries.values()].find((entry) => entry.id !== ignoredId && entry.driveId === destination.driveId && entry.path === join(destination.path, name)); }
function uniqueName(state: MockState, destination: Location, name: string, kind: Entry['kind']): string {
  const dot = kind === 'file' ? name.lastIndexOf('.') : -1;
  const base = dot > 0 ? name.slice(0, dot) : name; const ext = dot > 0 ? name.slice(dot) : '';
  for (let index = 2; index < 10000; index++) { const candidate = `${base} (${index})${ext}`; if (!collision(state, destination, candidate)) return candidate; }
  throw domainError('CONFLICT', 'No available Keep both name could be found');
}
function requireShape(request: OperationRequest): void {
  const targets = request.targets ?? [];
  if (request.kind === 'new-folder' && (!request.destination || !request.name || targets.length)) throw domainError('INVALID_REQUEST', 'New folder requires one name and a destination');
  if (request.kind === 'rename' && (targets.length !== 1 || !request.name)) throw domainError('INVALID_REQUEST', 'Rename requires exactly one target and a name');
  if (['copy', 'move'].includes(request.kind) && (!request.destination || !targets.length)) throw domainError('INVALID_REQUEST', 'Copy and move require targets and a destination');
  if (request.kind === 'upload' && request.selection && new Set(request.selection.items.map((item) => item.relativePath)).size !== request.selection.items.length) throw domainError('INVALID_REQUEST', 'The synthetic upload contains duplicate relative paths');
  if (request.kind === 'upload' && (!request.destination || !request.selection || request.selection.kind === 'download-destination' || request.selection.items.length === 0)) throw domainError('INVALID_REQUEST', 'Upload requires a nonempty synthetic source selection and a destination');
  if (request.kind === 'download' && (!targets.length || request.selection?.kind !== 'download-destination')) throw domainError('INVALID_REQUEST', 'Download requires targets and a synthetic destination selection');
  if (['trash', 'restore', 'purge'].includes(request.kind) && !targets.length) throw domainError('INVALID_REQUEST', 'Choose at least one explicit target');
  if (new Set(targets.map(targetKey)).size !== targets.length) throw domainError('INVALID_REQUEST', 'The target list contains duplicates');
  for (const target of targets) if (targets.some((other) => other.id !== target.id && other.driveId === target.driveId && within(target.path, other.path))) throw domainError('INVALID_REQUEST', 'Select a folder or its children, not both');
  if (request.kind === 'purge') {
    const confirmation = request.confirmation;
    const expected = targets.map(targetKey).sort(); const actual = confirmation?.targets.map(targetKey).sort();
    if (!confirmation || !confirmation.acknowledged || confirmation.kind !== 'permanent-delete' || targets.some((ref) => ref.driveId !== confirmation.driveId) || JSON.stringify(actual) !== JSON.stringify(expected)) throw domainError('CONFIRMATION_REQUIRED', 'Permanent deletion requires a separate acknowledgment of these exact current targets and their drive');
  }
}
function preflight(state: MockState, request: OperationRequest): OperationConflict[] {
  requireShape(request); actorFor(state, request);
  if (state.status.backend !== 'connected') throw domainError('UNAVAILABLE', 'The simulated backend is disconnected; unsafe mutations are disabled', true);
  const targets = request.targets ?? [];
  const fromTrash = request.kind === 'restore' || request.kind === 'purge';
  for (const ref of targets) {
    const entry = targetEntry(state, ref, fromTrash); requireDrive(state, ref.driveId, !['copy', 'download'].includes(request.kind));
    requireScope(state, request, ref, request.kind === 'purge' ? 'purge' : request.kind === 'trash' ? 'trash' : ['copy', 'download'].includes(request.kind) ? 'read' : 'write');
    if (['symlink', 'unsupported'].includes(entry.kind)) throw domainError('UNSUPPORTED', 'Symbolic links and special entries are not followed or transferred');
    if (!fromTrash && request.kind in entry.capabilities && !entry.capabilities[request.kind as keyof Entry['capabilities']]) throw domainError('PERMISSION_DENIED', entry.capabilities.reason ?? 'This action is not permitted for this item');
    if (request.destination && request.destination.driveId === ref.driveId && (request.destination.path === ref.path || entry.kind === 'directory' && within(request.destination.path, ref.path))) throw domainError('INVALID_PATH', 'A folder cannot be copied or moved into itself or a descendant');
    if (request.kind === 'move' && request.destination?.driveId === ref.driveId && request.destination.path === entry.parentPath) throw domainError('INVALID_PATH', 'This item is already in the selected destination');
    if (request.kind === 'rename' && request.name === entry.name) throw domainError('INVALID_REQUEST', 'Choose a different name');
  }
  if (request.destination) { requireDrive(state, request.destination.driveId, true); requireScope(state, request, request.destination, 'write'); requireParent(state, request.destination); }
  if (state.scenario === 'source-changed' && ['copy', 'move', 'download'].includes(request.kind)) throw domainError('SOURCE_CHANGED', 'The simulated source changed. Refresh it before starting.', true, 'mock-source', ['refresh']);
  const conflicts: OperationConflict[] = [];
  const inspect = (name: string, destination: Location, kind: Entry['kind'], ignoredId?: string) => { const existing = collision(state, destination, name, ignoredId); if (existing) conflicts.push({ sourceName: name, destination, existing: refOf(existing), kind: existing.kind !== kind ? 'type' : request.kind === 'restore' ? 'restore' : 'name', allowedPolicies: existing.kind !== kind ? ['ask', 'skip', 'keep-both'] : ['ask', 'skip', 'keep-both', 'replace'] }); };
  if (request.kind === 'new-folder') inspect(request.name!, request.destination!, 'directory');
  if (request.kind === 'rename') { const entry = targetEntry(state, targets[0]!); inspect(request.name!, { driveId: entry.driveId, path: entry.parentPath }, entry.kind, entry.id); }
  if (['copy', 'move'].includes(request.kind)) for (const ref of targets) { const entry = targetEntry(state, ref); inspect(entry.name, request.destination!, entry.kind); }
  if (request.kind === 'upload') for (const item of request.selection!.items) inspect(item.relativePath.split('/').at(-1)!, { driveId: request.destination!.driveId, path: join(request.destination!.path, parentOf(item.relativePath)) }, 'file');
  if (request.kind === 'restore') for (const ref of targets) {
    const item = state.trash.get(ref.id)!; const location = request.destination ?? { driveId: ref.driveId, path: parentOf(item.originalPath) };
    requireDrive(state, location.driveId, true); requireScope(state, request, location, 'write'); if (state.drives.find((drive) => drive.id === location.driveId)?.state === 'full') throw domainError('FULL_DISK', 'The simulated restore destination is full', true);
    if (!request.recreateParents) requireParent(state, location); inspect(item.originalPath.split('/').at(-1)!, location, item.entry.kind);
  }
  const policy = request.conflictPolicy ?? 'ask';
  if (policy === 'replace' && conflicts.some((conflict) => targets.some((target) => target.id === conflict.existing.id))) throw domainError('CONFLICT', 'An item cannot replace itself. Choose Keep both or a different destination.');
  if (policy === 'replace' && conflicts.some((conflict) => !conflict.allowedPolicies.includes('replace'))) throw domainError('CONFLICT', 'A file/folder type collision cannot be replaced. Choose Skip or Keep both.');
  const destinationDrive = state.drives.find((drive) => drive.id === request.destination?.driveId);
  if (destinationDrive?.state === 'full' && !['trash', 'purge'].includes(request.kind)) throw domainError('FULL_DISK', 'The simulated destination is full. No existing item or Trash was removed.', true, 'mock-full', ['choose-destination']);
  return conflicts;
}
function bytesFor(state: MockState, request: OperationRequest): string {
  if (['new-folder', 'rename', 'trash', 'restore', 'purge'].includes(request.kind)) return '0';
  if (request.kind === 'upload') return String(request.selection!.items.reduce((total, item) => total + BigInt(item.sizeBytes), 0n));
  return String((request.targets ?? []).reduce((total, ref) => { const entry = targetEntry(state, ref); return total + BigInt(entry.sizeBytes ?? '0') + descendants(state, entry).reduce((sum, child) => sum + BigInt(child.sizeBytes ?? '0'), 0n); }, 0n));
}
function putInTrash(state: MockState, entry: Entry, actor: Actor): void {
  const children = descendants(state, entry); const retainedBytes = entry.kind === 'directory' ? String(children.reduce((sum, item) => sum + BigInt(item.sizeBytes ?? '0'), 0n)) : entry.sizeBytes;
  const removed = { ...entry, revision: entry.revision + 1 };
  const record: TrashEntry = { entry: removed, originalPath: entry.path, deletedAt: state.clock.now(), deletedBy: actor, retainedBytes };
  state.trash.set(entry.id, record); state.trashContents.set(entry.id, children); state.entries.delete(entry.id); for (const child of children) state.entries.delete(child.id);
}
function ensureParents(state: MockState, destination: Location): void {
  const parts = destination.path.split('/').filter(Boolean); let path = '';
  for (const part of parts) { path = join(path, part); const existing = [...state.entries.values()].find((entry) => entry.driveId === destination.driveId && entry.path === path); if (existing && existing.kind !== 'directory') throw domainError('CONFLICT', 'A file blocks the required parent directory'); if (!existing) { const entry = createEntry(`entry-${++state.sequence}`, destination.driveId, path, 'directory'); entry.createdAt = state.clock.now(); entry.modifiedAt = state.clock.now(); state.entries.set(entry.id, entry); } }
}
function apply(state: MockState, request: OperationRequest, job: Job): void {
  const actor = actorFor(state, request); const policy = request.conflictPolicy ?? 'ask'; let applied = 0; let skipped = 0;
  function destinationName(name: string, destination: Location, kind: Entry['kind'], ignoredId?: string): string | null {
    const existing = collision(state, destination, name, ignoredId); if (!existing) return name;
    if (policy === 'skip') { skipped++; return null; }
    if (policy === 'keep-both') return uniqueName(state, destination, name, kind);
    if (policy === 'replace' && existing.kind === kind) { putInTrash(state, existing, actor); return name; }
    throw domainError('CONFLICT', 'A destination changed before commit. Choose a conflict policy for the current item.', true, `mock-${job.id}`, ['resolve-conflict']);
  }
  const refs = request.targets ?? [];
  const limit = state.scenario === 'partial-batch' && refs.length > 1 ? refs.length - 1 : refs.length;
  if (request.kind === 'new-folder') { const name = destinationName(request.name!, request.destination!, 'directory'); if (name) { const entry = createEntry(`entry-${++state.sequence}`, request.destination!.driveId, join(request.destination!.path, name), 'directory'); entry.createdAt = state.clock.now(); entry.modifiedAt = state.clock.now(); state.entries.set(entry.id, entry); job.resultingNames.push(name); applied++; } }
  else if (request.kind === 'rename') { const entry = targetEntry(state, refs[0]!); const name = destinationName(request.name!, { driveId: entry.driveId, path: entry.parentPath }, entry.kind, entry.id); if (name) { const oldPath = entry.path; const children = descendants(state, entry); entry.name = name; entry.path = join(entry.parentPath, name); entry.revision++; entry.modifiedAt = state.clock.now(); for (const child of children) { child.path = entry.path + child.path.slice(oldPath.length); child.parentPath = parentOf(child.path); child.revision++; } job.resultingNames.push(name); applied++; } }
  else if (request.kind === 'copy' || request.kind === 'move') for (const ref of refs.slice(0, limit)) {
    const entry = targetEntry(state, ref); const name = destinationName(entry.name, request.destination!, entry.kind); if (!name) continue;
    const children = descendants(state, entry); const destinationPath = join(request.destination!.path, name); const sameDriveMove = request.kind === 'move' && entry.driveId === request.destination!.driveId;
    for (const source of [entry, ...children]) { const path = destinationPath + source.path.slice(entry.path.length); const copy: Entry = { ...structuredClone(source), id: sameDriveMove ? source.id : `entry-${++state.sequence}`, driveId: request.destination!.driveId, path, parentPath: parentOf(path), name: path.split('/').at(-1)!, revision: sameDriveMove ? source.revision + 1 : 1, modifiedAt: state.clock.now() }; state.entries.set(copy.id, copy); }
    if (request.kind === 'move' && !sameDriveMove) { if (state.scenario === 'copied-source-remains') job.outcome = 'copied-source-remains'; else putInTrash(state, entry, actor); }
    job.resultingNames.push(name); applied++;
  }
  else if (request.kind === 'upload') for (const item of request.selection!.items) {
    const destination = { driveId: request.destination!.driveId, path: join(request.destination!.path, parentOf(item.relativePath)) }; ensureParents(state, destination);
    const name = destinationName(item.relativePath.split('/').at(-1)!, destination, 'file'); if (!name) continue; const entry = createEntry(`entry-${++state.sequence}`, destination.driveId, join(destination.path, name), 'file', item.sizeBytes); entry.createdAt = state.clock.now(); entry.modifiedAt = state.clock.now(); state.entries.set(entry.id, entry); job.resultingNames.push(join(parentOf(item.relativePath), name)); applied++;
  }
  else if (request.kind === 'download') { job.resultingNames = refs.map((ref) => targetEntry(state, ref).name); applied = refs.length; }
  else if (request.kind === 'trash') for (const ref of refs.slice(0, limit)) { const entry = targetEntry(state, ref); putInTrash(state, entry, actor); job.resultingNames.push(entry.name); applied++; }
  else if (request.kind === 'restore') for (const ref of refs.slice(0, limit)) {
    const item = state.trash.get(ref.id)!; const destination = request.destination ?? { driveId: ref.driveId, path: parentOf(item.originalPath) }; if (request.recreateParents) ensureParents(state, destination);
    const name = destinationName(item.originalPath.split('/').at(-1)!, destination, item.entry.kind); if (!name) continue;
    const newPath = join(destination.path, name); const oldPath = item.entry.path;
    for (const source of [item.entry, ...(state.trashContents.get(ref.id) ?? [])]) { const path = newPath + source.path.slice(oldPath.length); const restored = { ...source, driveId: destination.driveId, path, name: path.split('/').at(-1)!, parentPath: parentOf(path), revision: source.revision + 1 }; state.entries.set(restored.id, restored); }
    state.trash.delete(ref.id); state.trashContents.delete(ref.id); job.resultingNames.push(name); applied++;
  }
  else if (request.kind === 'purge') for (const ref of refs.slice(0, limit)) { const item = state.trash.get(ref.id)!; job.resultingNames.push(item.entry.name); state.trash.delete(ref.id); state.trashContents.delete(ref.id); applied++; }
  if (limit < refs.length || skipped && applied) job.outcome = 'partial'; else if (!applied && skipped) job.outcome = 'skipped';
  state.directoryRevision++; state.log(`Simulated ${request.kind} committed with ${applied} changed item(s) and ${skipped} skipped item(s).`);
}

/** Phase 7/8 simulation only. No source contents are ever read or written. */
export function createOperationPort(state: MockState): OperationPort {
  const commitRaces = new WeakSet<Job>();
  state.validateJob = (job) => { const request = state.plans.get(job.id); if (request) preflight(state, request); };
  state.commitJob = (job) => {
    const request = state.plans.get(job.id); if (!request) return;
    const conflicts = preflight(state, request);
    if ((conflicts.length && (request.conflictPolicy ?? 'ask') === 'ask') || state.scenario === 'commit-conflict' && !commitRaces.has(job)) { commitRaces.add(job); job.state = 'needs-decision'; job.error = domainError('CONFLICT', 'The destination changed at simulated commit. Review the current destination before retrying.', true, `mock-${job.id}`, ['resolve-conflict', 'cancel']).detail; state.emitJob(job); return; }
    // Copy-on-write makes simulated commit all-or-nothing unless a named partial scenario is selected.
    const entries = state.entries; const trash = state.trash; const contents = state.trashContents; const directoryRevision = state.directoryRevision;
    state.entries = new Map([...entries].map(([id, entry]) => [id, structuredClone(entry)])); state.trash = new Map([...trash].map(([id, entry]) => [id, structuredClone(entry)])); state.trashContents = new Map([...contents].map(([id, items]) => [id, structuredClone(items)]));
    try { apply(state, request, job); } catch (error) { state.entries = entries; state.trash = trash; state.trashContents = contents; state.directoryRevision = directoryRevision; job.resultingNames = []; throw error; }
  };
  return {
    submit: async (input) => {
      const parsed = operationRequestSchema.safeParse(input); if (!parsed.success) return { status: 'rejected', requestId: typeof input?.requestId === 'string' && input.requestId ? input.requestId : 'invalid-request', error: domainError('INVALID_REQUEST', 'Review the operation fields and choose valid logical names and targets').detail };
      const request = parsed.data; const fingerprint = JSON.stringify(request); const previous = state.requests.get(request.requestId);
      if (previous) {
        if (previous.fingerprint === fingerprint) return immutable(previous.receipt);
        const oldRequest = JSON.parse(previous.fingerprint) as OperationRequest;
        const sameTargets = JSON.stringify({ ...oldRequest, conflictPolicy: undefined }) === JSON.stringify({ ...request, conflictPolicy: undefined });
        if (sameTargets && request.conflictPolicy && request.conflictPolicy !== 'ask' && previous.receipt.status === 'needs-decision') state.requests.delete(request.requestId);
        else if (sameTargets && request.conflictPolicy && request.conflictPolicy !== 'ask' && previous.receipt.status === 'accepted' && previous.receipt.jobIds.every((id) => state.jobs.get(id)?.state === 'needs-decision')) {
          try { preflight(state, request); for (const id of previous.receipt.jobIds) { const job = state.jobs.get(id)!; state.plans.set(id, structuredClone(request)); job.state = 'preparing'; job.error = null; state.emitJob(job); } state.requests.set(request.requestId, { fingerprint, receipt: previous.receipt }); return immutable(previous.receipt); } catch (error) { return { status: 'rejected', requestId: request.requestId, error: safeError(error) }; }
        } else return { status: 'rejected', requestId: request.requestId, error: domainError('INVALID_REQUEST', 'This request ID was already used for a different operation').detail };
      }
      if (state.jobs.size + (request.selection?.items.length ?? request.targets?.length ?? 1) + 1 > 5000) return { status: 'rejected', requestId: request.requestId, error: domainError('UNAVAILABLE', 'The bounded demo job registry is full. Reset demo data before starting more operations.').detail };
      if (state.requests.size >= 1000) return { status: 'rejected', requestId: request.requestId, error: domainError('UNAVAILABLE', 'The bounded demo request registry is full. Reset demo data before starting more operations.').detail };
      if (state.scenario === 'mutation-timeout-before') throw domainError('TIMEOUT', 'Simulated response timed out before acceptance. Reconcile this request ID before choosing whether to retry.', true, request.requestId, ['retry']);
      let receipt: OperationReceipt;
      try {
        const conflicts = preflight(state, request);
        if (conflicts.length && (request.conflictPolicy ?? 'ask') === 'ask') receipt = { status: 'needs-decision', requestId: request.requestId, conflicts };
        else {
          const id = `job-${++state.sequence}`; const names = request.targets?.map((ref) => (request.kind === 'restore' || request.kind === 'purge' ? state.trash.get(ref.id)?.entry : state.entries.get(ref.id))?.name ?? '') ?? [];
          const job: Job = { id, requestId: request.requestId, revision: 0, actor: actorFor(state, request), operation: request.kind, label: request.name ?? (request.kind === 'upload' ? request.selection!.label : names.length === 1 ? names[0]! : `${names.length} selected items`), source: request.targets?.[0] ? { driveId: request.targets[0].driveId, path: request.targets[0].path } : null, destination: request.destination ?? null, state: 'queued', bytesDone: '0', bytesTotal: bytesFor(state, request), rateBytesPerSecond: null, etaSeconds: null, capabilities: jobCapabilities('queued'), createdAt: state.clock.now(), updatedAt: state.clock.now(), parentId: null, childIds: [], previousAttemptId: null, error: null, outcome: null, resultingNames: [], simulated: true };
          const batchItems = request.kind === 'upload' ? request.selection!.items.map((item) => ({ label: item.relativePath, sizeBytes: item.sizeBytes, source: null })) : (request.targets ?? []).map((ref) => { const entry = request.kind === 'restore' || request.kind === 'purge' ? state.trash.get(ref.id)!.entry : state.entries.get(ref.id)!; return { label: entry.name, sizeBytes: entry.sizeBytes, source: { driveId: ref.driveId, path: ref.path } }; });
          if (batchItems.length > 1) for (const item of batchItems) {
            const childId = `job-${++state.sequence}`;
            const child: Job = { ...structuredClone(job), id: childId, requestId: `${request.requestId}:item:${job.childIds.length}`, parentId: id, childIds: [], label: item.label, source: item.source, bytesTotal: item.sizeBytes ?? '0', capabilities: { pause: false, resume: false, cancel: false, retry: false } };
            state.jobs.set(childId, child); job.childIds.push(childId);
          }
          state.plans.set(id, structuredClone(request)); state.emitJob(job); receipt = { status: 'accepted', requestId: request.requestId, jobIds: [id], simulated: true };
        }
      } catch (error) { receipt = { status: 'rejected', requestId: request.requestId, error: safeError(error) }; }
      const validated = operationReceiptSchema.parse(receipt); state.requests.set(request.requestId, { fingerprint, receipt: immutable(validated) });
      if (state.scenario === 'mutation-timeout-after' && receipt.status === 'accepted') throw domainError('TIMEOUT', 'Simulated response timed out after acceptance. Reconcile this request ID; do not resubmit blindly.', true, request.requestId, ['retry']);
      return immutable(validated);
    },
    reconcile: async (requestId) => immutable(state.requests.get(requestId)?.receipt ?? null),
  };
}
