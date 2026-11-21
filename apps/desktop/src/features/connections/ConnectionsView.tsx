import { useEffect, useRef, useState } from 'react';
import { Activity, ArrowUpRight, Cable, CircleDot, KeyRound, Network, RefreshCw, Radio, ShieldCheck } from 'lucide-react';
import { configurationSchema, safeError } from '../../contracts';
import type { AgentSummary, BackendPorts, Drive, ServiceStatus } from '../../contracts';
import { Button, Dialog } from '../../shared/ui';
import { DiagnosticsView } from '../diagnostics';
import { AgentList } from './AgentList';
import { AgentScopeDialog } from './AgentScopeDialog';
import { ServicePanel } from './ServicePanel';
import './connections.css';

export function ConnectionsView({ ports }: { ports: BackendPorts }) {
  const demo=ports.mode==='mock';
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [statusError, setStatusError] = useState(false);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentLoading, setAgentLoading] = useState(true);
  const [agentError, setAgentError] = useState('');
  const [endpoint, setEndpoint] = useState<string | undefined>(undefined);
  const [drives, setDrives] = useState<Drive[]>([]);
  const [attempt, setAttempt] = useState(0);
  const [editing, setEditing] = useState<AgentSummary | null>(null);
  const [revoking, setRevoking] = useState<AgentSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [mutationError, setMutationError] = useState('');
  const [mutationBlocked, setMutationBlocked] = useState(false);
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const pending = useRef(false);
  useEffect(() => {
    const request = ++generation.current;
    const controller = new AbortController();
    let receivedEvent = false;
    setAgentLoading(true); setAgentError(''); setStatusError(false);
    pending.current = false; setBusy(false);
    const unsubscribe = ports.status.subscribe(value => { if (request === generation.current) { receivedEvent = true; setStatus(value); setStatusError(false); } });
    ports.status.get({ signal: controller.signal }).then(value => { if (request === generation.current && !receivedEvent && !controller.signal.aborted) setStatus(value); })
      .catch(() => { if (request === generation.current && !receivedEvent && !controller.signal.aborted) setStatusError(true); });
    ports.agents.list({ signal: controller.signal }).then(value => { if (request === generation.current && !controller.signal.aborted) setAgents(value); })
      .catch(error => { if (request === generation.current && !controller.signal.aborted) setAgentError(safeError(error).code === 'PERMISSION_DENIED' ? 'Owner access is required to manage agent identities.' : 'Agent identities could not be read. This does not change service status.'); })
      .finally(() => { if (request === generation.current && !controller.signal.aborted) setAgentLoading(false); });
    ports.configuration.get({ signal: controller.signal }).then(value => { const config = configurationSchema.parse(value); if (request === generation.current && !controller.signal.aborted) setEndpoint(config.health.endpoint); })
      .catch(() => { if (request === generation.current && !controller.signal.aborted) setEndpoint(undefined); });
    ports.drives.list({ signal: controller.signal }).then(value => { if (request === generation.current && !controller.signal.aborted) setDrives(value); })
      .catch(() => { if (request === generation.current && !controller.signal.aborted) setDrives([]); });
    return () => { controller.abort(); unsubscribe(); generation.current++; };
  }, [ports, attempt]);
  const unavailable = status?.backend !== 'connected' || status.stale || statusError;
  function openAgent(agent: AgentSummary, action: 'scopes' | 'revoke') { setMutationError(''); setMutationBlocked(false); setNotice(''); if (action === 'scopes') setEditing(agent); else setRevoking(agent); }
  function reloadAgents() { setEditing(null); setRevoking(null); setMutationError(''); setMutationBlocked(false); setAttempt(value => value + 1); }
  async function mutate(action: () => Promise<AgentSummary>) {
    if (pending.current || unavailable || mutationBlocked) return;
    const request = generation.current;
    pending.current = true; setBusy(true); setMutationError('');
    try {
      const agent = await action();
      if (request !== generation.current) return;
      setAgents(current => current.map(item => item.id === agent.id ? agent : item)); setEditing(null); setRevoking(null);
      setNotice(`${agent.label}: ${demo?'demo ':''}${agent.revoked ? 'identity revoked' : 'scopes updated'}. Other identities and the owner are unchanged.`);
    } catch (error) {
      if (request !== generation.current) return;
      const code = safeError(error).code;
      const blocked = ['TIMEOUT', 'STALE_REVISION', 'PERMISSION_REVOKED'].includes(code);
      setMutationBlocked(blocked);
      setMutationError(blocked ? 'The identity state may have changed. Reload agents before another action; no automatic retry was sent.' : code === 'PERMISSION_DENIED' ? 'Owner permission is required. No identity was changed.' : 'The change could not be completed. Other identities remain unchanged.');
    } finally { if (request === generation.current) { pending.current = false; setBusy(false); } }
  }
  return <section className="connections-view" aria-labelledby="connections-title"><header className="connections-heading"><div><span className="eyebrow">Interfaces / identities</span><h2 id="connections-title">Connections</h2><p>One storage layer. Independent service boundaries.</p></div><Button size="small" icon={<RefreshCw size={14}/>} disabled={busy || agentLoading} onClick={() => setAttempt(value => value + 1)}>Refresh status</Button></header>
    <div className="connections-backend"><span><CircleDot size={15}/><strong>Local backend</strong><span>{status ? `${status.backend === 'connected' ? 'Connected' : status.backend === 'reconnecting' ? 'Reconnecting' : 'Unavailable'}${demo?' · simulated':''}` : 'Status unknown'}</span></span><span>{demo?'Demo data · no real services connected':'Real service mode'}</span></div>
    {(statusError || status?.stale) && <p role="alert" className="connection-error">Connection status is unavailable or stale. Cached values are informational; identity changes are disabled.</p>}
    <div className="connections-services"><ServicePanel simulated={demo} title="Transfer API" subtitle="Authenticated access for remote clients" icon={Network} state={status?.api} stale={status?.stale || statusError}/><ServicePanel simulated={demo} title="MCP server" subtitle="Capability-scoped tools for agents" icon={Cable} state={status?.mcp} stale={status?.stale || statusError}/><ServicePanel simulated={demo} title="Health WebSocket" subtitle="Outbound service and device telemetry" icon={Radio} state={status?.health} outbound endpoint={endpoint} stale={status?.stale || statusError}/></div>
    <div className="connections-session-strip"><Activity size={18}/><div><strong>Live authenticated sessions</strong><p>Current sessions are separate from registered identities.</p></div><span className="connections-session-count">{status ? status.authenticatedSessions : '—'}</span><span className="connections-session-demo">{demo?'simulated':'service'}{status?.stale || statusError ? ' · stale' : ''}</span></div>
    <section className="connections-identities" aria-labelledby="agents-title"><header><div><span className="eyebrow">Owner access</span><h3 id="agents-title"><KeyRound size={18}/> Registered agent identities</h3><p>{demo?'Separate scopes and revocation for every agent. No real credentials are generated or stored.':'Separate scopes and revocation for every agent. Credentials stay outside the renderer.'}</p></div><span className="connections-count">{agents.length} registered</span></header>
      {agentLoading && <p role="status">{demo?'Reading demo identities…':'Reading registered identities…'}</p>}{agentError && <p role="alert" className="connection-error">{agentError}</p>}{notice && <p role="status" className="connection-notice">{notice}</p>}
      {!agentLoading && !agentError && !agents.length && <p>{demo?'No demo identities registered.':'No identities registered.'}</p>}
      {!agentError && <AgentList agents={agents} disabled={unavailable || agentLoading || busy} onEdit={agent => openAgent(agent, 'scopes')} onRevoke={agent => openAgent(agent, 'revoke')}/>}
    </section>
    <div className="connections-safety"><ShieldCheck size={17}/><p>{demo?'Credential creation is a nonfunctional layout placeholder. There is no secret input, copy-token action, listener, or real connection test.':'Enrollment and credential storage are administered outside this renderer. Review scopes before granting access.'}</p><ArrowUpRight size={16}/></div>
    <details className="connections-diagnostics"><summary>Redacted diagnostics <span>{demo?'Bounded synthetic activity':'Bounded redacted service activity'}</span></summary><DiagnosticsView simulated={demo} port={ports.diagnostics}/></details>
    {editing && <AgentScopeDialog key={editing.id} agent={editing} drives={drives} busy={busy} error={mutationError} blocked={mutationBlocked} onClose={() => { if (!busy) setEditing(null); }} onReload={reloadAgents} onSave={(scopes, roots) => void mutate(() => ports.agents.updateScopes(editing.id, editing.revision, scopes, roots))}/>}
    {revoking && <Dialog open title={`Revoke ${revoking.label}?`} description={`This revokes only ${revoking.label} (${revoking.id}) ${demo?'in the mock dataset':'in the service'}. The owner and other agent identities keep their current access.`} dismissible={!busy} onClose={() => { if (!busy) setRevoking(null); }} footer={<><Button disabled={busy} onClick={() => setRevoking(null)}>Keep identity</Button>{mutationBlocked ? <Button onClick={reloadAgents}>Reload agents</Button> : <Button variant="danger" disabled={busy || unavailable} onClick={() => void mutate(() => ports.agents.revoke(revoking.id, revoking.revision))}>{busy ? 'Revoking…' : (demo?'Revoke demo identity':'Revoke identity')}</Button>}</>}><p>{demo?'Simulated jobs attributed to this identity stop at their next permission checkpoint. No real credential exists.':'Jobs attributed to this identity stop at their next permission checkpoint. Existing access is revoked.'}</p>{mutationError && <p role="alert" className="connection-error">{mutationError}</p>}</Dialog>}
  </section>;
}
