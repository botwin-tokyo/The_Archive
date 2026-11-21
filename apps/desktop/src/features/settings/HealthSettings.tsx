import { Radio } from 'lucide-react';
import type { Configuration } from '../../contracts';
import { Button } from '../../shared/ui';
import { endpointHasPrivateParts, type SettingsErrors } from './draft';
interface Props { simulated?:boolean; value: Configuration['health']; errors: SettingsErrors; onChange: (value: Configuration['health']) => void; endpointRejected: string; onEndpointRejected: (value: string) => void; onTest: () => void; testing: boolean; testDisabled: boolean; testResult: string; }
export function HealthSettings({ value, errors, onChange, endpointRejected, onEndpointRejected, onTest, testing, testDisabled, testResult, simulated=true }: Props) {
  const endpointError = endpointRejected || errors['health.endpoint'];
  return <section className="settings-section" aria-labelledby="health-settings-title"><div className="settings-section-heading"><Radio size={19}/><div><h3 id="health-settings-title">Health reporting</h3><p>Outbound WebSocket reporting is independent of storage, API, and MCP.</p></div></div>
    <label className="settings-check-row"><span>{simulated?'Enable simulated reporting':'Enable health reporting'}<small>{simulated?'No network connection is opened':'Uses saved outbound service configuration'}</small></span><input type="checkbox" checked={value.enabled} onChange={event => onChange({ ...value, enabled: event.target.checked })}/></label>
    <label className="settings-block-label" htmlFor="health-endpoint">{simulated?'Synthetic endpoint':'Health endpoint'}<input id="health-endpoint" type="text" autoComplete="off" spellCheck={false} placeholder="wss://monitor.example.invalid/health" maxLength={500} value={value.endpoint} aria-invalid={Boolean(endpointError)} aria-describedby="health-endpoint-help health-endpoint-error" onChange={event => {
      const next = event.target.value;
      if (endpointHasPrivateParts(next)) { onEndpointRejected('Credentials, query strings, and fragments are not accepted. Do not enter real secrets.'); return; }
      onEndpointRejected(''); onChange({ ...value, endpoint: next });
    }}/></label>
    <p className="settings-help" id="health-endpoint-help">{simulated?'Use a reserved .invalid host for this demo. Authentication is Not configured; real credentials cannot be entered.':'Use a secure WebSocket endpoint. Authentication secrets must be configured outside the renderer.'}</p>
    <p className="settings-field-error" id="health-endpoint-error" role={endpointError ? 'alert' : undefined}>{endpointError || ''}</p>
    <label className="settings-select-row" htmlFor="health-interval"><span>Send interval<small>{simulated?'Seconds between synthetic samples':'Seconds between bounded health samples'}</small></span><input id="health-interval" type="number" min={5} max={3600} value={Number.isNaN(value.intervalSeconds) ? '' : value.intervalSeconds} onChange={event => onChange({ ...value, intervalSeconds: event.target.value === '' ? NaN : Number(event.target.value) })} aria-invalid={Boolean(errors['health.intervalSeconds'])} aria-describedby={errors['health.intervalSeconds'] ? 'health-interval-error' : undefined}/></label>
    {errors['health.intervalSeconds'] && <p id="health-interval-error" className="settings-field-error">{errors['health.intervalSeconds']}</p>}
    <div className="settings-health-test"><Button size="small" disabled={testing || testDisabled} onClick={onTest}>{testing ? (simulated?'Simulating…':'Testing…') : (simulated?'Simulate connection test':'Test connection')}</Button><span>{simulated?'Uses saved demo settings only':'Uses saved service settings only'}</span></div>
    {testResult && <p className="settings-notice" role="status">{testResult}</p>}
  </section>;
}
