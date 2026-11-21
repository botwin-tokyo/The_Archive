import { ArrowLeftRight } from 'lucide-react';
export function TransferSettings({ concurrency, savedConcurrency, onChange, simulated=true }: { simulated?:boolean;concurrency: number; savedConcurrency: number; onChange: (value: number) => void }) {
  return <section className="settings-section" aria-labelledby="transfer-settings-title"><div className="settings-section-heading"><ArrowLeftRight size={19}/><div><h3 id="transfer-settings-title">Transfers</h3><p>Bounded work keeps the console responsive.</p></div></div>
    <label className="settings-select-row" htmlFor="transfer-concurrency"><span>Concurrent transfers<small>{simulated?'Demo preference, 1–4 jobs':'Service profile, 1–2 jobs'}</small></span><select id="transfer-concurrency" value={concurrency} onChange={event => onChange(Number(event.target.value))}>{(simulated?[1, 2, 3, 4]:[1,2]).map(value => <option key={value} value={value}>{value}</option>)}</select></label>
    {concurrency !== savedConcurrency && <p className="settings-notice">{simulated?'Restart required after this change in a future integrated service. Saving this demo will not restart anything or interrupt active jobs.':'The service reports whether this change needs restart. Saving will not automatically restart it.'}</p>}
  </section>;
}
