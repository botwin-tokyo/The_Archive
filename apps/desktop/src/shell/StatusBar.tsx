import type {ServiceStatus} from '../contracts';
import { CircleHelp, ShieldCheck } from 'lucide-react';

export function StatusBar({ onRoute,status,mode='mock' }: { onRoute: (route: string) => void;mode?:'mock'|'service';status?:ServiceStatus|null }) {
 const display=(value:string)=>value[0].toUpperCase()+value.slice(1).replaceAll('-',' ');
  return <footer className="app-status" aria-label="Application status">
    <span className="status-version">AC <span>0.1.0</span></span>
    <span className="status-divider"/>
    <span className="status-demo"><span className="status-dot"/>{mode==='mock'?'Demo backend':'Service backend'}{status?.backend&&status.backend!=='connected'?`: ${display(status.backend)}`:''}</span>
    <button type="button" onClick={() => onRoute('connections')}><span className="status-dot is-neutral"/>API: {display(status?.api??'not-configured')}</button>
    <button type="button" onClick={() => onRoute('connections')}><span className="status-dot is-neutral"/>MCP: {display(status?.mcp??'not-configured')}</button>
    <button type="button" onClick={() => onRoute('settings')} className="status-health"><span className="status-dot is-neutral"/>Health WS: {display(status?.health??'not-configured')}</button>
    <span className="status-right"><ShieldCheck size={13}/><span>{mode==='mock'?'No real storage connected':status?.backend==='connected'?'Authenticated service':'Service unavailable or unconfigured'}</span></span>
    <span className="status-help" title={mode==='mock'?'This prototype only changes synthetic, in-memory data':'Real storage is controlled by the independent service'}><CircleHelp size={13}/></span>
  </footer>;
}
