import { useEffect, useRef, useState } from 'react';
import { FileText, RefreshCw, ShieldCheck } from 'lucide-react';
import type { DiagnosticsPort } from '../../contracts';
import { Button } from '../../shared/ui';
import { DIAGNOSTIC_LIMIT, redactDiagnostics, type SafeDiagnostic } from './redact';
import './diagnostics.css';

export function DiagnosticsView({ port,simulated=true }: { port: DiagnosticsPort;simulated?:boolean }) {
  const [rows, setRows] = useState<SafeDiagnostic[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const generation = useRef(0);
  useEffect(() => {
    const request = ++generation.current;
    const controller = new AbortController();
    setLoading(true); setError(false);
    port.list({ signal: controller.signal }).then(result => {
      if (request === generation.current && !controller.signal.aborted) setRows(redactDiagnostics(result,simulated).reverse());
    }).catch(() => { if (request === generation.current && !controller.signal.aborted) setError(true); })
      .finally(() => { if (request === generation.current && !controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); generation.current++; };
  }, [port, attempt,simulated]);
  return <section className="diagnostics-view" aria-labelledby="diagnostics-title">
    <header className="diagnostics-heading"><div><span className="eyebrow">{simulated?'Local observability / demo':'Local service observability'}</span><h2 id="diagnostics-title"><FileText size={20}/> Diagnostics</h2></div>
      <Button size="small" icon={<RefreshCw size={15}/>} disabled={loading} onClick={() => setAttempt(value => value + 1)}>Refresh logs</Button></header>
    <p className="diagnostics-policy"><ShieldCheck size={16}/> {simulated?'Allowlisted synthetic events only.':'Allowlisted service events only.'} Latest {DIAGNOSTIC_LIMIT} records maximum. Raw messages, paths, credentials, and request payloads are omitted.</p>
    {loading && <p role="status">{simulated?'Reading synthetic diagnostics…':'Reading redacted service diagnostics…'}</p>}
    {error && <div className="diagnostics-error" role="alert">Diagnostics could not be read. {rows.length > 0 && 'Showing the last snapshot; it may be stale.'} Other panels remain available.</div>}
    {!loading && !error && !rows.length && <p className="diagnostics-empty">{simulated?'No synthetic events recorded.':'No diagnostic events recorded.'}</p>}
    {rows.length > 0 && <ol className="diagnostics-list" aria-label="Redacted diagnostic events">{rows.map((row, index) => <li key={`${row.at}-${row.correlationId}-${index}`}>
      <div className="diagnostics-event-meta"><span className={`diagnostics-level diagnostics-level--${row.level}`}>{row.level}</span><time>{row.at === 'Unknown' ? 'Time unknown' : new Date(row.at).toLocaleString('en-GB', { timeZone: 'UTC' }) + ' UTC'}</time></div>
      <p>{row.message}</p><div className="diagnostics-event-code"><span>{row.code}</span><span>Correlation: {row.correlationId}</span></div>
    </li>)}</ol>}
    <p className="diagnostics-footnote">{simulated?'In-memory demo history. No diagnostic export or upload is performed.':'Bounded service history. No diagnostic export or upload is performed.'}</p>
  </section>;
}
