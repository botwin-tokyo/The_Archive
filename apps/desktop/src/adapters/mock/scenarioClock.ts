import { domainError } from '../../contracts/errors';
import { FIXTURE_TIME } from '../../fixtures/drives';
/** A deterministic scheduler: no native timers, wall-clock sampling, network, or I/O. */
export class ScenarioClock {
  private value: number;
  private nextId = 0;
  private tasks = new Map<number, { due: number; run: () => void; cancel?: () => void }>();
  private listeners = new Set<(milliseconds: number) => void>();
  constructor(start = FIXTURE_TIME) { this.value = Date.parse(start); }
  now(): string { return new Date(this.value).toISOString(); }
  get pendingCount(): number { return this.tasks.size; }
  subscribe(listener: (milliseconds: number) => void): () => void { this.listeners.add(listener); return () => this.listeners.delete(listener); }
  advance(milliseconds: number): void {
    if (!Number.isFinite(milliseconds) || milliseconds < 0 || milliseconds > 86_400_000) throw new RangeError('Advance requires 0–86400000 milliseconds');
    this.value += milliseconds;
    for (const [id, task] of this.tasks) if (task.due <= this.value) { this.tasks.delete(id); task.run(); }
    for (const listener of [...this.listeners]) listener(milliseconds);
  }
  delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return Promise.reject(domainError('ABORTED', 'This read was canceled'));
    if (milliseconds === 0) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const id = ++this.nextId;
      const abort = () => { this.tasks.delete(id); signal?.removeEventListener('abort', abort); reject(domainError('ABORTED', 'This read was canceled')); };
      this.tasks.set(id, { due: this.value + milliseconds, run: () => { signal?.removeEventListener('abort', abort); resolve(); }, cancel: abort });
      signal?.addEventListener('abort', abort, { once: true });
    });
  }
  reset(): void { for (const task of this.tasks.values()) task.cancel?.(); this.tasks.clear(); this.value = Date.parse(FIXTURE_TIME); }
  dispose(): void { this.reset(); this.listeners.clear(); }
}
