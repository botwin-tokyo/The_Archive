import { KeyRound, Shield, UserRound, Ban } from 'lucide-react';
import type { AgentSummary } from '../../contracts';
import { Button } from '../../shared/ui';
export function AgentList({ agents, disabled, onEdit, onRevoke }: { agents: AgentSummary[]; disabled: boolean; onEdit: (agent: AgentSummary) => void; onRevoke: (agent: AgentSummary) => void }) {
  return <div className="connection-agent-list">{agents.map(agent => {const demo=agent.credentialDisplay.startsWith('DEMO');return <article className={`connection-agent ${agent.revoked ? 'connection-agent--revoked' : ''}`} key={agent.id} aria-label={`${agent.label}${demo?' demo':''} identity`}>
    <header><span className="connection-agent-icon"><UserRound size={20}/></span><div><h4>{agent.label}</h4><span className="connection-agent-id">{agent.id}</span></div><span className={`connection-agent-status ${agent.revoked ? 'is-revoked' : ''}`}>{agent.revoked ? <Ban size={13}/> : <Shield size={13}/>} {agent.revoked ? (demo?'Revoked · demo':'Revoked') : (demo?'Active · demo':'Active')}</span></header>
    <div className="connection-agent-scopes">{agent.scopes.length ? agent.scopes.map(scope => <span key={scope}>{scope}</span>) : <span>No scopes</span>}</div>
    <dl><dt>Allowed roots</dt><dd>{agent.allowedRoots.length ? agent.allowedRoots.map(root => <span key={`${root.driveId}/${root.path}`}>{root.driveId} / {root.path || 'Root'}</span>) : 'No roots permitted'}</dd><dt>Last used</dt><dd>{agent.lastUsedAt ? new Date(agent.lastUsedAt).toLocaleString('en-GB', { timeZone: 'UTC' }) + (demo?' UTC · simulated':' UTC') : 'Never used'}</dd></dl>
    <p className="connection-placeholder"><KeyRound size={13}/> {agent.credentialDisplay}</p>
    <footer><Button size="small" aria-label={`Edit scopes for ${agent.label}`} disabled={disabled || agent.revoked} onClick={() => onEdit(agent)}>Edit scopes</Button><Button size="small" aria-label={`Revoke ${agent.label}`} variant="danger" disabled={disabled || agent.revoked} onClick={() => onRevoke(agent)}>Revoke</Button></footer>
  </article>;})}</div>;
}
