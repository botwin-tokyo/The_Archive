import { describe, expect, it } from 'vitest';
import { DIAGNOSTIC_LIMIT, redactDiagnostic, redactDiagnostics } from './redact';
const event = { id: 'diagnostic-1', at: '2026-09-30T09:00:00.000Z', level: 'info', code: 'DEMO_EVENT', message: 'Untrusted raw event text', correlationId: 'mock-1', simulated: true };
describe('diagnostic allowlist', () => {
  it('reconstructs safe records and does not retain raw messages or unknown fields', () => {
    const secret = 'test-only-' + 'x'.repeat(32);
    const result = redactDiagnostic({ ...event, id: secret, message: `Bearer ${secret} /private/${secret}.txt`, stack: secret, headers: { Authorization: secret }, credentials: secret, payload: { token: secret }, fileName: secret });
    expect(result).toEqual({ at: event.at, level: 'info', code: 'DEMO_EVENT', message: 'Synthetic activity recorded.', correlationId: 'mock-1' });
    expect(JSON.stringify(result)).not.toContain(secret);
  });
  it('redacts sentinels smuggled into every displayed field', () => {
    const result = redactDiagnostic({ ...event, at: 'TIME_SECRET', level: 'LEVEL_SECRET', code: 'CODE_SECRET', correlationId: 'mock-CORRELATION_SECRET' });
    expect(result).toEqual({ at: 'Unknown', level: 'info', code: 'INVALID_RESPONSE', message: 'A response failed validation.', correlationId: 'Redacted' });
    expect(JSON.stringify(result)).not.toContain('SECRET');
  });
  it('rejects real/non-object records and retains only the latest bounded window', () => {
    expect(redactDiagnostic(null)).toBeNull(); expect(redactDiagnostic({ ...event, simulated: false })).toBeNull();
    const result = redactDiagnostics(Array.from({ length: 10000 }, (_, index) => ({ ...event, correlationId: `mock-${index}` })));
    expect(result).toHaveLength(DIAGNOSTIC_LIMIT); expect(result[0].correlationId).toBe('mock-9900'); expect(result.at(-1)?.correlationId).toBe('mock-9999');
  });
  it('uses a fixed safe message for every recognized error', () => {
    expect(redactDiagnostic({ ...event, code: 'PERMISSION_REVOKED', message: 'token=BAD' })?.message).toBe('A simulated identity was revoked.');
  });
});
