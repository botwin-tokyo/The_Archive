import { useEffect, useRef, useState } from 'react';
import { LockKeyhole, Save, Settings2 } from 'lucide-react';
import { configurationSchema, safeError } from '../../contracts';
import type { BackendPorts, Configuration, Drive, Job } from '../../contracts';
import { Button } from '../../shared/ui';
import { DriveSettings } from './DriveSettings';
import { TransferSettings } from './TransferSettings';
import { HealthSettings } from './HealthSettings';
import { AppearanceSettings } from './AppearanceSettings';
import { editableSettings, validateSettingsDraft } from './draft';
import './settings.css';

export interface SettingsViewProps { ports: BackendPorts; onDirtyChange?: (dirty: boolean) => void; onSaved?: () => void; }
export function SettingsView({ ports, onDirtyChange, onSaved }: SettingsViewProps) {
  const demo=ports.mode==='mock';
  const [saved, setSaved] = useState<Configuration | null>(null);
  const [draft, setDraft] = useState<Configuration | null>(null);
  const [drives, setDrives] = useState<Drive[]>([]);
  const [driveError, setDriveError] = useState(false);
  const [lastHarnessTransfer, setLastHarnessTransfer] = useState<string | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [busy, setBusy] = useState<'save' | 'check' | 'test' | null>(null);
  const [message, setMessage] = useState('');
  const [saveBlocked, setSaveBlocked] = useState(false);
  const [checked, setChecked] = useState<Configuration | null>(null);
  const [endpointRejected, setEndpointRejected] = useState('');
  const [testResult, setTestResult] = useState('');
  const generation = useRef(0);
  const pending = useRef(false);
  const callbacks = useRef({ onDirtyChange, onSaved });
  callbacks.current = { onDirtyChange, onSaved };
  const dirty = Boolean(draft && saved && editableSettings(draft) !== editableSettings(saved));
  const errors = draft ? validateSettingsDraft(draft) : {};
  useEffect(() => { callbacks.current.onDirtyChange?.(dirty); }, [dirty]);
  useEffect(() => () => { callbacks.current.onDirtyChange?.(false); }, []);
  useEffect(() => {
    const request = ++generation.current;
    const controller = new AbortController();
    setLoadError(''); setSaved(null); setDraft(null); setSaveBlocked(false); setChecked(null);
    pending.current = false; setBusy(null); setMessage(''); setEndpointRejected(''); setTestResult('');
    ports.configuration.get({ signal: controller.signal }).then(value => {
      const config = configurationSchema.parse(value);
      if (request === generation.current && !controller.signal.aborted) { setSaved(config); setDraft(config); }
    }).catch(error => {
      if (request === generation.current && !controller.signal.aborted) setLoadError(safeError(error).code === 'PERMISSION_DENIED' ? 'Owner access is required to view these settings.' : 'Settings could not be loaded. Files and jobs remain independent.');
    });
    ports.drives.list({ signal: controller.signal }).then(value => { if (request === generation.current && !controller.signal.aborted) { setDrives(value); setDriveError(false); } })
      .catch(() => { if (request === generation.current && !controller.signal.aborted) setDriveError(true); });
    const completed = (job: Job) => job.actor.id === 'agent-harness' && job.operation === 'upload' && job.state === 'completed';
    let liveCompletion: string | null = null;
    const unsubscribe = ports.jobs.subscribe(event => {
      if (request === generation.current && completed(event.job)) { liveCompletion = event.job.updatedAt; setLastHarnessTransfer(old => !old || old < event.job.updatedAt ? event.job.updatedAt : old); }
    });
    ports.jobs.snapshot({ signal: controller.signal }).then(snapshot => {
      const last = snapshot.jobs.filter(completed).map(job => job.updatedAt).sort().at(-1) ?? null;
      if (request === generation.current && !controller.signal.aborted) setLastHarnessTransfer(last && (!liveCompletion || last > liveCompletion) ? last : liveCompletion);
    }).catch(() => { /* Missing transfer history must not block settings. */ });
    return () => { controller.abort(); unsubscribe(); generation.current++; };
  }, [ports, loadAttempt]);

  function edit(next: Configuration) { setDraft(next); setTestResult(''); if (!saveBlocked) setMessage(''); }
  async function save() {
    if (!draft || !dirty || pending.current || saveBlocked || endpointRejected || Object.keys(errors).length) return;
    const request = generation.current;
    pending.current = true; setBusy('save'); setMessage('');
    try {
      const result = configurationSchema.parse(await ports.configuration.save(draft));
      if (request !== generation.current) return;
      setSaved(result); setDraft(result); setChecked(null); setSaveBlocked(false);
      setMessage(demo?'Demo settings saved for this session. No connection or restart was performed.':'Service settings saved. No automatic restart was performed.');
      callbacks.current.onSaved?.();
    } catch (error) {
      if (request !== generation.current) return;
      const code = safeError(error).code;
      if (code === 'TIMEOUT') { setSaveBlocked(true); setMessage('Save timed out. The outcome is uncertain. Your draft is preserved. Check saved state before trying again.'); }
      else if (code === 'STALE_REVISION') { setSaveBlocked(true); setMessage('Saved settings changed elsewhere. Your draft is preserved. Check saved state, then review before saving.'); }
      else setMessage(code === 'PERMISSION_DENIED' || code === 'PERMISSION_REVOKED' ? 'Save refused: owner permission is required. Your draft is preserved.' : 'Settings could not be saved. Your draft is preserved.');
    } finally { if (request === generation.current) { pending.current = false; setBusy(null); } }
  }
  async function checkSaved() {
    if (!draft || !saved || pending.current) return;
    const request = generation.current;
    pending.current = true; setBusy('check');
    try {
      const latest = configurationSchema.parse(await ports.configuration.get());
      if (request !== generation.current) return;
      if (editableSettings(latest) === editableSettings(draft)) {
        setSaved(latest); setDraft(latest); setSaveBlocked(false); setChecked(null);
        setMessage('The saved snapshot matches your draft. No repeat save was sent.'); callbacks.current.onSaved?.();
      } else if (latest.revision === saved.revision && editableSettings(latest) === editableSettings(saved)) {
        setSaved(latest); setDraft({ ...draft, revision: latest.revision }); setSaveBlocked(false); setChecked(null);
        setMessage('Saved state checked: your changes were not saved. The draft is preserved and can be submitted explicitly.');
      } else { setChecked(latest); setMessage('A different saved version was found. Review it below before choosing which settings to keep.'); }
    } catch { if (request === generation.current) setMessage('Saved state could not be checked. Your draft is preserved; saving remains blocked.'); }
    finally { if (request === generation.current) { pending.current = false; setBusy(null); } }
  }
  async function testHealth() {
    if (dirty || saveBlocked || !saved?.health.endpoint || pending.current) return;
    const request = generation.current;
    pending.current = true; setBusy('test'); setTestResult('');
    try {
      const result = await ports.configuration.testHealth();
      if (request === generation.current) setTestResult(result.simulated?(result.success?'Simulated connection test passed. No network connection was made.':'Simulated connection test failed. Storage remains available; no network connection was made.'):result.message);
    } catch { if (request === generation.current) setTestResult(demo?'The simulated test could not finish. Storage and transfers are unaffected.':'The health test could not finish. Storage and transfers are independent.'); }
    finally { if (request === generation.current) { pending.current = false; setBusy(null); } }
  }
  return <section className="settings-view" aria-labelledby="settings-title">
    <header className="settings-heading"><div><span className="eyebrow">Owner console / configuration</span><h2 id="settings-title"><Settings2 size={23}/> Settings</h2><p>{demo?'Synthetic preferences. No real storage or services are configured here.':'Service settings use saved revisions. Credentials and physical mounts are configured separately.'}</p></div><span className="settings-owner"><LockKeyhole size={14}/> Local owner · demo</span></header>
    {!draft || !saved ? loadError ? <div className="settings-notice" role="alert"><p>{loadError}</p><Button onClick={() => setLoadAttempt(value => value + 1)}>Retry settings</Button></div> : <p role="status">{demo?'Loading demo settings…':'Loading service settings…'}</p> : <>
      <div className="settings-save-bar"><div><span className={`settings-draft-dot ${dirty ? 'is-dirty' : ''}`}/><strong>{dirty ? 'Unsaved draft' : 'Saved snapshot'}</strong><span>Revision {saved.revision}</span></div><div><Button disabled={!dirty || Boolean(busy)} onClick={() => { setDraft(saved); setChecked(null); setEndpointRejected(''); setTestResult(''); setMessage(saveBlocked ? 'Draft canceled. Check saved state to resolve the previous save outcome.' : 'Changes canceled.'); }}>Cancel changes</Button><Button variant="primary" icon={<Save size={15}/>} disabled={!dirty || Boolean(busy) || saveBlocked || Boolean(endpointRejected) || Boolean(Object.keys(errors).length)} onClick={() => void save()}>{busy === 'save' ? 'Saving…' : (demo?'Save demo settings':'Save settings')}</Button></div></div>
      {message && <div role="status" className={`settings-notice ${saveBlocked ? 'settings-notice--warning' : ''}`}>{message}</div>}
      {saveBlocked && <Button disabled={Boolean(busy)} onClick={() => void checkSaved()}>{busy === 'check' ? 'Checking…' : 'Check saved state'}</Button>}
      {checked && <section className="settings-checked" aria-label="Latest saved settings"><h3>Latest saved snapshot · revision {checked.revision}</h3><dl><dt>Drive labels</dt><dd>{Object.values(checked.driveLabels).join(' · ')}</dd><dt>Concurrent transfers</dt><dd>{checked.transferConcurrency}</dd><dt>Health reporting</dt><dd>{checked.health.enabled ? (demo?'Enabled · simulated':'Enabled') : 'Disabled'}</dd><dt>Endpoint</dt><dd>{checked.health.endpoint || 'Not configured'}</dd><dt>Send interval</dt><dd>{checked.health.intervalSeconds} seconds</dd><dt>Reduced motion</dt><dd>{checked.appearance.reducedMotion ? 'Enabled' : 'Disabled'}</dd><dt>Compact rows</dt><dd>{checked.appearance.compactRows ? 'Enabled' : 'Disabled'}</dd></dl><p>Keeping the draft will replace the editable values above when you explicitly save.</p><div><Button onClick={() => { setSaved(checked); setDraft(checked); setChecked(null); setSaveBlocked(false); setEndpointRejected(''); setMessage('Loaded the saved version. Draft discarded.'); }}>Use saved settings</Button><Button onClick={() => { setSaved(checked); setDraft({ ...draft, revision: checked.revision }); setChecked(null); setSaveBlocked(false); setMessage('Draft kept against the latest revision. Review every field before saving.'); }}>Keep my draft</Button></div></section>}
      {saved.restartRequired && <p className="settings-notice settings-notice--warning">{demo?'A service restart would be required in the integrated product. Restart is deferred; no restart control is active in this demo.':'A service restart is required. Restart is deferred and must be performed by an administrator.'}</p>}
      <fieldset className="settings-fields" disabled={Boolean(busy)}><DriveSettings draft={draft} drives={drives} driveError={driveError} lastHarnessTransfer={lastHarnessTransfer} errors={errors} onLabel={(id, label) => edit({ ...draft, driveLabels: { ...draft.driveLabels, [id]: label } })}/>
        <div className="settings-lower-grid"><div><TransferSettings simulated={demo} concurrency={draft.transferConcurrency} savedConcurrency={saved.transferConcurrency} onChange={transferConcurrency => edit({ ...draft, transferConcurrency })}/><AppearanceSettings simulated={demo} value={draft.appearance} onChange={appearance => edit({ ...draft, appearance })}/></div><HealthSettings simulated={demo} value={draft.health} errors={errors} onChange={health => edit({ ...draft, health })} endpointRejected={endpointRejected} onEndpointRejected={setEndpointRejected} onTest={() => void testHealth()} testing={busy === 'test'} testDisabled={dirty || saveBlocked || !saved.health.endpoint} testResult={testResult}/></div>
      </fieldset><p className="settings-footer">{demo?'Service access and per-agent scopes are shown in Connections. No physical volume remapping, real credentials, or service restart is available in this frontend demo.':'Service access and per-agent scopes are shown in Connections. Volume mounting, credentials and service restart are administered separately.'}</p>
    </>}
  </section>;
}
