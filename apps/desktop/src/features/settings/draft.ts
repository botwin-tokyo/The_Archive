import { configurationSchema } from '../../contracts';
import type { Configuration } from '../../contracts';
export type SettingsErrors = Record<string, string>;
export function editableSettings(value: Configuration): string {
  return JSON.stringify({ driveLabels: value.driveLabels, transferConcurrency: value.transferConcurrency, health: value.health, appearance: value.appearance });
}
export function validateSettingsDraft(draft: Configuration): SettingsErrors {
  const result = configurationSchema.safeParse(draft);
  const errors: SettingsErrors = {};
  if (!result.success) for (const issue of result.error.issues) {
    const key = issue.path.join('.') || 'health.endpoint';
    errors[key] = key === 'health.endpoint' ? 'Use a wss:// endpoint without credentials, query strings, or fragments.' : key.startsWith('driveLabels.') ? 'Enter a display label between 1 and 120 characters.' : key === 'health.intervalSeconds' ? 'Choose an interval between 5 and 3600 seconds.' : 'Review this setting.';
  }
  for (const [id, label] of Object.entries(draft.driveLabels)) if (!label.trim()) errors[`driveLabels.${id}`] = 'Enter a display label between 1 and 120 characters.';
  if (draft.health.enabled && !draft.health.endpoint) errors['health.endpoint'] = draft.simulated?'A synthetic endpoint is required when reporting is enabled.':'A secure WebSocket endpoint is required when reporting is enabled.';
  if (draft.simulated && draft.health.endpoint && !errors['health.endpoint']) {
    const url = new URL(draft.health.endpoint);
    if (!url.hostname.endsWith('.invalid') || url.hostname === '.invalid') errors['health.endpoint'] = 'For this demo, use a reserved .invalid host, such as wss://monitor.example.invalid/health.';
  }
  return errors;
}
/** Reject secret-bearing endpoint forms before they enter the local draft. */
export function endpointHasPrivateParts(value: string): boolean { return /[@?#]/.test(value) || Array.from(value).some(character => character.charCodeAt(0) < 32) || /\b(?:password|secret|token|authorization)\s*[=:]/i.test(value); }
