import { useState } from 'react';
import type { AgentSummary, Drive } from '../../contracts';
import { logicalPathSchema } from '../../contracts';
import { Button, Dialog } from '../../shared/ui';
const ALL_SCOPES: AgentSummary['scopes'] = ['read', 'write', 'trash', 'purge', 'settings', 'acquire', 'radio', 'vault.read', 'vault.reveal', 'vault.write', 'vault.export','skills.read','skills.write','phone_transfer.send','phone_transfer.read','phone_transfer.delete'];
export function AgentScopeDialog({ agent, drives, busy, error, blocked, onSave, onClose, onReload }: { agent: AgentSummary; drives: Drive[]; busy: boolean; error: string; blocked: boolean; onSave: (scopes: AgentSummary['scopes'], roots: AgentSummary['allowedRoots']) => void; onClose: () => void; onReload: () => void }) {
  const demo=agent.credentialDisplay.startsWith('DEMO');
  const [scopes, setScopes] = useState(agent.scopes);
  const [roots, setRoots] = useState(agent.allowedRoots);
  const invalid = roots.some(root => !logicalPathSchema.safeParse(root.path).success || !drives.some(drive => drive.id === root.driveId));
  const changed = JSON.stringify(scopes) !== JSON.stringify(agent.scopes) || JSON.stringify(roots) !== JSON.stringify(agent.allowedRoots);
  return <Dialog open title={`Edit ${agent.label} scopes`} description={`Only ${agent.label} (${agent.id}) is affected. ${demo?'These are in-memory demo permissions; no real credential exists.':'These permissions affect real service access.'}`} onClose={onClose} dismissible={!busy} footer={<><Button disabled={busy} onClick={onClose}>Cancel</Button>{blocked ? <Button onClick={onReload}>Reload agents</Button> : <Button variant="primary" disabled={busy || invalid || !changed} onClick={() => onSave(scopes, roots)}>{busy ? 'Saving…' : (demo?'Save demo scopes':'Save scopes')}</Button>}</>}>
    <fieldset className="agent-scope-fields" disabled={busy || blocked}><legend>Allowed capabilities</legend><div className="agent-scope-options">{ALL_SCOPES.filter(scope=>demo||scope!=='settings').map(scope => <label key={scope}><input type="checkbox" checked={scopes.includes(scope)} onChange={event => setScopes(event.target.checked ? [...scopes, scope] : scopes.filter(item => item !== scope))}/>{scope}</label>)}</div>
    <h3>Allowed logical roots</h3><p>Empty path means that drive’s root. Absolute system paths and traversal are rejected.</p>{drives.map(drive => {
      const root = roots.find(item => item.driveId === drive.id);
      return <div key={drive.id} className="agent-root"><label><input type="checkbox" checked={Boolean(root)} onChange={event => setRoots(event.target.checked ? [...roots, { driveId: drive.id, path: '' }] : roots.filter(item => item.driveId !== drive.id))}/>Allow {drive.label}</label>{root && <label className="agent-root-path">Path within {drive.label}<input value={root.path} placeholder="Root" maxLength={4096} aria-invalid={!logicalPathSchema.safeParse(root.path).success} onChange={event => setRoots(roots.map(item => item.driveId === drive.id ? { ...item, path: event.target.value } : item))}/></label>}</div>;
    })}{!drives.length && <p>Drive identities are unavailable. Reload Connections before editing roots.</p>}{invalid && <p role="alert" className="connection-error">Use valid relative paths on the known drive identities. Changes have not been submitted.</p>}</fieldset>
    {error && <p className="connection-error" role="alert">{error}</p>}
  </Dialog>;
}
