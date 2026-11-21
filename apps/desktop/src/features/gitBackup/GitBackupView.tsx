import {useCallback,useEffect,useState} from 'react';
import type {BackendPorts,Location} from '../../contracts';
import {safeError} from '../../contracts';
import type {GitStatus,GitSettings} from '../../contracts/gitBackup';
import './gitBackup.css';
const date=(value?:number|null)=>value?new Date(value*1000).toLocaleString():'Not yet';
export function GitBackupView({ports,onNavigate}:{ports:BackendPorts;onNavigate:(location:Location)=>void}){
 const [status,setStatus]=useState<GitStatus|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const api=ports.gitBackup;
 const refresh=useCallback(async()=>{if(!api)return;try{setStatus(await api.status());}catch(e){setError(safeError(e).message);}},[api]);
 useEffect(()=>{void refresh();const timer=setInterval(()=>void refresh(),5000);return()=>clearInterval(timer);},[refresh]);
 async function action(fn:()=>Promise<unknown>){setBusy(true);setError('');try{await fn();await refresh();}catch(e){setError(safeError(e).message);}finally{setBusy(false);}}
 async function run(retry=false){if(!api)return;const id='git-ui-'+crypto.randomUUID();await action(async()=>{try{await (retry?api.retry(id):api.run(id));}catch(e){if(!await api.reconcile(id))throw e;}});}
 async function configure(patch:Partial<GitSettings>){if(api&&status)await action(()=>api.configure({...status.settings,...patch}));}
 const sourceBytes=status?.repositories.filter(repo=>repo.published).reduce((total,repo)=>total+(repo.bytes??0),0)??0;
 const active=status?.run&&['queued','running','publishing'].includes(status.run.state);
 return <section className="git-backup"><header><span className="eyebrow">Git volume · repository history and source files</span><h2>GitHub Backup</h2><p>Keep your repositories locally, updated every midnight in UTC time.</p></header>
 {!api&&<p>This feature requires the connected Archive service.</p>}
 {error&&<p role="alert">{error}</p>}
 {status&&<>
 {!status.configured&&<p role="status">The GitHub backup worker has not been installed. Existing files are unchanged.</p>}
 <div className="git-summary"><div><strong>{status.identity??'GitHub account'}</strong><p>{status.repositories.length} repositories · {status.repositories.filter(r=>r.published).length} source views saved</p><p>{(sourceBytes/1024**3).toLocaleString(undefined,{maximumFractionDigits:2})} GiB of current source files · Git history stored separately</p></div><div><strong>Last complete backup</strong><p>{date(status.lastSuccess?.at)}</p></div><div><strong>Next scheduled run</strong><p>{status.nextRun?new Date(status.nextRun).toLocaleString():'Schedule disabled'} · UTC</p></div></div>
 <div className="git-controls"><label>Repositories <select disabled={busy||!!active} value={status.settings.scope} onChange={e=>void configure({scope:e.target.value as GitSettings['scope']})}><option value="owned">Owned by my account</option><option value="controlled">All repositories under my control</option></select></label><label><input type="checkbox" checked={status.settings.enabled} disabled={busy||!status.configured} onChange={e=>void configure({enabled:e.target.checked})}/> Back up every midnight</label><button className="btn" disabled={busy||status.previewing||!status.configured} onClick={()=>void action(()=>api!.preview())}>Discover repositories</button><button className="btn primary" disabled={busy||!!active||!status.configured} onClick={()=>void run()}>Run backup now</button><button className="btn" disabled={busy||!!active||!status.configured} onClick={()=>void run(true)}>Retry failed or partial</button>{active&&<button className="btn" disabled={busy} onClick={()=>void action(()=>api!.cancel())}>Cancel run</button>}</div>
 {status.previewing&&<p role="status">Discovering accessible repositories…</p>}{status.preview&&<p>Discovery found {status.preview.repositories.length} repositories for {status.preview.login}. {status.preview.visibilityNotice}</p>}
 {status.run&&<p role="status">Run: <strong>{status.run.state}</strong> · {status.run.completed??0} / {status.run.total??0} acquired · {status.run.failed??0} failed{status.run.current?` · ${status.run.current}`:''}{status.run.error?` · ${status.run.error}`:''}</p>}
 {status.error&&<p role="alert">{status.error}</p>}
 <p>{status.coverageNotice}</p><p>Source views are generated and read-only. Use <strong>Copy to…</strong> in Files to make an editable copy. Full Git history is retained separately for restore.</p>
 <div className="git-table"><table><thead><tr><th>Repository</th><th>Branch / commit</th><th>Backup status</th><th>Files</th></tr></thead><tbody>{status.repositories.map(repo=><tr key={repo.id}><td>{repo.fullName}<details><summary>Branch override</summary><form onSubmit={e=>{e.preventDefault();const data=new FormData(e.currentTarget);const branch=String(data.get('branch')??'').trim();const branchOverrides={...status.settings.branchOverrides};if(branch)branchOverrides[repo.id]=branch;else delete branchOverrides[repo.id];void configure({branchOverrides});}}><input name="branch" aria-label={`Branch for ${repo.fullName}`} defaultValue={status.settings.branchOverrides[repo.id]??''} placeholder="Follow GitHub default"/><button className="btn" disabled={busy||!!active}>Save</button></form></details></td><td>{repo.selectedBranch??'—'}<br/><code>{repo.commit?.slice(0,12)}</code></td><td>{repo.status??repo.availability}{repo.availability==='not-discovered'&&' · no longer visible; retained'}{repo.lastError&&<p>{repo.lastError}</p>}{!!repo.coverage?.length&&<details><summary>{repo.coverage.length} coverage exceptions</summary>{repo.coverage.map((c,i)=><p key={i}>{c.path}: {c.message}</p>)}</details>}</td><td>{repo.published&&repo.path?<button className="btn" onClick={()=>onNavigate({driveId:'git',path:repo.path!})}>Open files</button>:'Not published'}</td></tr>)}</tbody></table></div>
 <details><summary>Run history</summary>{[...status.history].sort((a,b)=>b.startedAt-a.startedAt).map(run=><p key={run.id}>{date(run.startedAt)} · {run.state} · {run.completed??0} repositories acquired</p>)}</details>
 </>}
 </section>;
}
