import type { Job, JobEvent, JobSnapshot } from '../../contracts';
/** Pure consumer reconciliation. An event gap requests a snapshot; older revisions never win. */
export function reconcileJobEvent(snapshot: JobSnapshot, event: JobEvent): { snapshot: JobSnapshot; needsSnapshot: boolean } {
  if (event.cursor <= snapshot.cursor) return { snapshot, needsSnapshot: false };
  if (event.cursor !== snapshot.cursor + 1) return { snapshot, needsSnapshot: true };
  const old = snapshot.jobs.find((job) => job.id === event.job.id);
  const jobs = old && old.revision >= event.job.revision ? snapshot.jobs : old ? snapshot.jobs.map((job) => job.id === event.job.id ? event.job : job) : [...snapshot.jobs, event.job];
  return { snapshot: { ...snapshot, cursor: event.cursor, jobs }, needsSnapshot: false };
}
export function mergeJobSnapshot(previous: JobSnapshot, incoming: JobSnapshot): JobSnapshot {
  if (incoming.cursor < previous.cursor) return previous;
  const previousById = new Map(previous.jobs.map((job) => [job.id, job]));
  const jobs = incoming.jobs.map((job) => { const old = previousById.get(job.id); return old && old.revision > job.revision ? old : job; });
  const present = new Set(jobs.map((job) => job.id));
  for (const job of previous.jobs) if (!present.has(job.id) && ['completed', 'failed', 'canceled'].includes(job.state)) jobs.push(job);
  return { ...incoming, jobs };
}
export function isTerminalJob(job: Job): boolean { return ['completed', 'failed', 'canceled'].includes(job.state); }
