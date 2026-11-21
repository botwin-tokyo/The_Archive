import { ArrowDownLeft, ArrowUpRight, type LucideIcon } from 'lucide-react';
import type { ServiceStatus } from '../../contracts';
export type ConnectionState = ServiceStatus['api'] | ServiceStatus['mcp'] | ServiceStatus['health'];
const labels: Record<ConnectionState, string> = { 'not-configured': 'Not configured', ready:'Ready', connected:'Connected', 'simulated-ready': 'Simulated Ready', 'simulated-connected': 'Simulated Connected', starting: 'Simulated Starting', connecting: 'Simulated Connecting', reconnecting: 'Simulated Reconnecting', disabled: 'Disabled', error: 'Simulated Error' };
export function ServicePanel({ title, subtitle, icon: Icon, state, outbound = false, endpoint, stale = false, simulated = true }: { title: string; subtitle: string; icon: LucideIcon; state?: ConnectionState; outbound?: boolean; endpoint?: string; stale?: boolean; simulated?:boolean }) {
  return <section className="connection-service" aria-label={title}><header><span className="connection-service-icon"><Icon size={22}/></span><span className="connection-direction">{outbound ? <ArrowUpRight size={14}/> : <ArrowDownLeft size={14}/>} {outbound ? 'Outbound' : 'Inbound'}</span></header><h3>{title}</h3><p>{subtitle}</p>
    <div className={`connection-state connection-state--${state ?? 'unknown'}`}><span aria-hidden="true"/>{state ? simulated?labels[state]:labels[state].replace('Simulated ','') : 'Status unknown'}{stale && ' · stale'}</div>
    <dl><dt>{outbound ? 'Endpoint' : 'Listener address'}</dt><dd>{outbound ? endpoint === undefined ? 'Unknown' : endpoint || 'Not configured' : !state ? 'Unknown' : state === 'not-configured' || state === 'disabled' ? 'Not configured' : (simulated?'Not provided by demo':'Managed by service')}</dd><dt>Authentication</dt><dd>{simulated?'Not configured':'Managed by service'}</dd><dt>Transport</dt><dd>{simulated?'Not connected · demo only':'Service-reported status'}</dd></dl>
    {state === 'reconnecting' && <p className="connection-service-note">{simulated?'Simulated retry state. Storage, API, and MCP stay independent.':'Reconnecting. Storage, API, and MCP stay independent.'}</p>}
    {state === 'error' && <p className="connection-service-note">{simulated?'A simulated service error was reported. No real listener or network connection exists.':'The service reported an error in this connection.'}</p>}
  </section>;
}
