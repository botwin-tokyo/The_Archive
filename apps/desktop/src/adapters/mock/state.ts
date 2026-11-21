import type { AgentSummary, Configuration, Diagnostic, Drive, Entry, Job, JobEvent, OperationReceipt, OperationRequest, ServiceStatus, TrashEntry } from '../../contracts';
import type { MockScenario } from '../../fixtures/scenarios';
import type { ScenarioClock } from './scenarioClock';
export interface MockState {
  clock: ScenarioClock; scenario: MockScenario; drives: Drive[]; entries: Map<string, Entry>; jobs: Map<string, Job>; trash: Map<string, TrashEntry>; trashContents: Map<string, Entry[]>; agents: AgentSummary[]; configuration: Configuration; status: ServiceStatus; diagnostics: Diagnostic[]; directoryRevision: number; cursor: number; sequence: number; requests: Map<string, { fingerprint: string; receipt: OperationReceipt }>; plans: Map<string, OperationRequest>; eventListeners: Set<(event: JobEvent) => void>; statusListeners: Set<(status: ServiceStatus) => void>; emitJob: (job: Job) => void; commitJob: (job: Job) => void; validateJob: (job: Job) => void; log: (message: string, code?: Diagnostic['code']) => void;
}
export function immutable<T>(value: T): T {
  const clone = structuredClone(value);
  const freeze = (node: unknown): void => { if (node && typeof node === 'object' && !Object.isFrozen(node)) { Object.freeze(node); for (const child of Object.values(node)) freeze(child); } };
  freeze(clone); return clone;
}
