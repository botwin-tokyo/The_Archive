import { errorCodeSchema } from '../../contracts';
import type { Diagnostic } from '../../contracts';

export const DIAGNOSTIC_LIMIT = 100;
const messages: Record<Diagnostic['code'], string> = {
  DEMO_EVENT: 'Synthetic activity recorded.', SERVICE_EVENT:'Service activity recorded.', UNAVAILABLE: 'A simulated service is unavailable.',
  TIMEOUT: 'A simulated request timed out.', PERMISSION_DENIED: 'The simulated identity lacks permission.',
  PERMISSION_REVOKED: 'A simulated identity was revoked.', READ_ONLY: 'A simulated drive is read only.',
  FULL_DISK: 'A simulated drive has insufficient space.', CONFLICT: 'A simulated operation needs a conflict decision.',
  STALE_REVISION: 'A saved revision changed.', INVALID_PATH: 'An invalid logical path was rejected.',
  NOT_FOUND: 'A simulated item was not found.', SOURCE_CHANGED: 'A simulated source changed.',
  INTERRUPTED: 'A simulated operation was interrupted.', INTEGRITY_FAILED: 'Simulated integrity verification failed.',
  UNSUPPORTED: 'An unsupported action was rejected.', ABORTED: 'A simulated request was canceled.',
  INVALID_RESPONSE: 'A response failed validation.', INVALID_REQUEST: 'A request failed validation.',
  CONFIRMATION_REQUIRED: 'An operation requires explicit confirmation.',
};
export interface SafeDiagnostic { at: string; level: Diagnostic['level']; code: Diagnostic['code']; message: string; correlationId: string; }

/** Do not copy raw messages, IDs, headers, payloads, stacks, file names, or unknown fields.
 * Correlation IDs are restricted to the mock adapter's documented non-secret vocabulary.
 * Rendering a fresh allowlisted record avoids regex-only secret redaction failures. */
export function redactDiagnostic(input: unknown,simulated=true): SafeDiagnostic | null {
  if (!input || typeof input !== 'object') return null;
  const value = input as Record<string, unknown>;
  if (value.simulated !== simulated) return null;
  const parsedCode = errorCodeSchema.safeParse(value.code);
  const code = value.code === 'SERVICE_EVENT'&&!simulated?'SERVICE_EVENT':value.code === 'DEMO_EVENT' ? 'DEMO_EVENT' : parsedCode.success ? parsedCode.data : 'INVALID_RESPONSE';
  const at = typeof value.at === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value.at) && Number.isFinite(Date.parse(value.at)) ? value.at : 'Unknown';
  const level = value.level === 'warning' || value.level === 'error' ? value.level : 'info';
  const correlationId = typeof value.correlationId === 'string' && (simulated?/^mock-(?:\d{1,12}|contract|validation|connection|metadata)$/:/^(?:[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/).test(value.correlationId) ? value.correlationId : 'Redacted';
  return { at, level, code, message: simulated?messages[code]:messages[code].replace(/simulated /gi,'').replace('Simulated integrity','Integrity'), correlationId };
}
export function redactDiagnostics(input: readonly unknown[],simulated=true): SafeDiagnostic[] {
  return input.slice(-DIAGNOSTIC_LIMIT).map(value=>redactDiagnostic(value,simulated)).filter((value): value is SafeDiagnostic => value !== null);
}
