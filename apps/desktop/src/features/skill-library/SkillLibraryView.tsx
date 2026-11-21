import {useEffect,useRef,useState} from 'react';
import Markdown from 'react-markdown';
import {BookOpen,FileText,RefreshCw,Download,Search} from 'lucide-react';
import type {BackendPorts} from '../../contracts';
import type {SkillEntry,SkillDetail} from '../../contracts/skills';
import './skills.css';

function message(error:unknown){return error instanceof Error?error.message:'The library request failed. Refresh and try again.';}
function relativeLink(href:string){
 try{const value=decodeURIComponent(href.split('#')[0]);if(!value||value.startsWith('/')||value.includes('\\')||value.includes(':'))return null;const parts=value.replace(/^\.\//,'').split('/');return parts.some(p=>!p||p==='.'||p==='..')?null:parts.join('/');}catch{return null;}
}
export function SkillLibraryView({ports}:{ports:BackendPorts}){
 const api=ports.skills;
 const [query,setQuery]=useState(''),[trashed,setTrashed]=useState(false),[refresh,setRefresh]=useState(0);
 const [items,setItems]=useState<SkillEntry[]>([]),[cursor,setCursor]=useState<string|null>(null),[selected,setSelected]=useState('');
 const [detail,setDetail]=useState<SkillDetail|null>(null),[revision,setRevision]=useState<number|undefined>(),[text,setText]=useState('');
 const [source,setSource]=useState(false),[status,setStatus]=useState('Loading'),[loading,setLoading]=useState(true),[reading,setReading]=useState(false),[busy,setBusy]=useState(false);
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[preview,setPreview]=useState<{path:string;text:string}|null>(null);
 const [key,setKey]=useState(''),[confirmTrash,setConfirmTrash]=useState(false);
 const listGeneration=useRef(0),detailGeneration=useRef(0);
 useEffect(()=>{if(!key)return;const timer=setTimeout(()=>setKey(''),60000);return()=>clearTimeout(timer);},[key]);
 useEffect(()=>{
  const generation=++listGeneration.current;if(!api){setLoading(false);setStatus('Unavailable');return;}
  setLoading(true);setError('');
  const timer=setTimeout(()=>{void Promise.all([api.status(),api.list({query,trashed,limit:50})]).then(([health,page])=>{
   if(generation!==listGeneration.current)return;setStatus(health.state);setItems(page.items);setCursor(page.nextCursor);setSelected(current=>page.items.some(e=>e.id===current)?current:page.items[0]?.id||'');
  }).catch(cause=>{if(generation===listGeneration.current){setError(message(cause));setStatus('unavailable');}}).finally(()=>{if(generation===listGeneration.current)setLoading(false);});},200);
  return()=>{clearTimeout(timer);listGeneration.current++;};
 },[api,query,trashed,refresh]);
 useEffect(()=>{
  const generation=++detailGeneration.current;setPreview(null);setConfirmTrash(false);setDetail(null);setText('');
  if(!api||!selected){setReading(false);return;}setReading(true);
  void api.get(selected,revision).then(async entry=>{const doc=await api.document(selected,entry.contentRevision);if(generation===detailGeneration.current){setDetail(entry);setText(doc.text);}}).catch(cause=>{if(generation===detailGeneration.current)setError(message(cause));}).finally(()=>{if(generation===detailGeneration.current)setReading(false);});
  return()=>{detailGeneration.current++;};
 },[api,selected,revision,refresh]);
 function choose(id:string){setRevision(undefined);setSelected(id);setSource(false);setError('');}
 async function action(work:()=>Promise<unknown>,success=''){setBusy(true);setError('');try{await work();if(success)setNotice(success);}catch(cause){setError(message(cause));}finally{setBusy(false);}}
 async function loadMore(){if(!api||!cursor)return;const generation=listGeneration.current;await action(async()=>{const page=await api.list({query,trashed,limit:50,cursor});if(generation===listGeneration.current){setItems(old=>[...old,...page.items.filter(e=>!old.some(x=>x.id===e.id))]);setCursor(page.nextCursor);}});}
 async function inspect(path:string){
  if(!api||!detail)return;const generation=detailGeneration.current;
  await action(async()=>{const file=await api.fileRead(detail.id,path,detail.contentRevision);const bytes=Uint8Array.from(atob(file.data),c=>c.charCodeAt(0));let content:string;try{content=new TextDecoder('utf-8',{fatal:true}).decode(bytes);}catch{content='Binary file. Use Download to save the original bytes.';}if(generation===detailGeneration.current)setPreview({path,text:content+(file.nextOffset!==null?'\n\n[Preview limited to the first 64 KiB]':'')});});
 }
 async function follow(href:string){
  if(!api||!detail)return;
  if(/^https?:\/\//i.test(href)){await action(()=>api.openLink(href));return;}
  const path=relativeLink(href);if(path&&detail.files.some(f=>f.path===path))await inspect(path);else setNotice('This link does not point to a file in this skill package.');
 }
 if(!api)return <section className="skill-library"><h1>Skill Library</h1><p>The Skill Library requires a connected Archive service.</p></section>;
 return <section className="skill-library" aria-label="Skill Library">
  <header className="skills-heading"><div><span className="eyebrow">AGENT WORKSPACE</span><h1><BookOpen size={25}/>Skill Library</h1><p>Your skills, instructions and supporting files in one place.</p></div><div className="skills-heading-actions"><span className="skills-status">{status}</span><button className="btn" onClick={()=>{setRevision(undefined);setRefresh(n=>n+1);}} disabled={busy}><RefreshCw size={15}/> Refresh</button></div></header>
  {error&&<div role="alert" className="notice notice--warning">{error}</div>}{notice&&<div role="status" className="notice">{notice}<button aria-label="Dismiss library notice" onClick={()=>setNotice('')}>×</button></div>}
  <div className="skills-layout"><aside className="skills-index" aria-label="Skill selection">
   <label className="skills-search"><Search size={17}/><input aria-label="Search skills" placeholder="Search skills…" value={query} maxLength={256} onChange={e=>{setQuery(e.target.value);setRevision(undefined);}}/></label>
   <label className="skills-trash-filter"><input type="checkbox" checked={trashed} onChange={e=>{setTrashed(e.target.checked);setRevision(undefined);}}/>Show trash</label>
   {loading?<p role="status">Loading skills…</p>:items.length===0?<div className="skills-empty"><BookOpen size={30}/><h2>{query?'No matching skills':trashed?'Trash is empty':'Your skill library is ready'}</h2><p>{query?'Try another name or description.':'Publish a skill from your app or agent using the API or MCP.'}</p></div>:<ul className="skills-list">{items.map(item=><li key={item.id}><button aria-pressed={selected===item.id} onClick={()=>choose(item.id)}><strong>{item.name}</strong><span>{item.description||item.slug}</span><small>Updated {new Date(item.updatedAt*1000).toLocaleDateString()}</small></button></li>)}</ul>}
   {cursor&&<button className="btn" disabled={busy||loading} onClick={()=>void loadMore()}>Load more skills</button>}
   <details className="skills-integration"><summary>Connect your app or agent</summary><p>Use the Archive MCP tool <code>skills.help</code> for upload instructions. The API is available at <code>/skills/v1</code>.</p><button className="btn" disabled={busy} onClick={()=>void action(async()=>{const result=await api.issueKey('Skill Library integration');setKey(result.secret);})}>Create integration key</button>{key&&<><p>Save this key now. It disappears here after one minute. Manage or revoke it in Connections.</p><textarea aria-label="New skill integration key" readOnly value={key}/><button className="btn" onClick={()=>setKey('')}>Hide key</button></>}</details>
  </aside><article className="skills-detail" aria-label="Selected skill">
   {reading?<p role="status">Reading skill…</p>:!detail?<div className="skills-empty"><FileText size={36}/><h2>Select a skill</h2><p>Read its instructions and browse its supporting files here.</p></div>:<>
    <header className="skills-detail-heading"><div><h2>{detail.name}</h2><p>{detail.description}</p><small>{detail.slug} · {detail.files.length} files</small></div><label>Revision <select aria-label="Skill revision" value={detail.contentRevision} disabled={busy} onChange={e=>setRevision(Number(e.target.value))}>{[...detail.revisions].reverse().map(value=><option key={value} value={value}>{value}</option>)}</select></label></header>
    {detail.warnings.map(w=><p key={w} className="skills-warning">{w}</p>)}
    <div className="skills-document-toolbar"><div role="group" aria-label="Document view"><button className="btn" aria-pressed={!source} onClick={()=>setSource(false)}>Rendered</button><button className="btn" aria-pressed={source} onClick={()=>setSource(true)}>Source</button></div><button className="btn" disabled={busy} onClick={()=>void action(()=>api.copy(detail.id,detail.contentRevision),'SKILL.md copied.')}>Copy SKILL.md</button></div>
    {source?<pre className="skills-source" tabIndex={0}>{text}</pre>:text.length>131072?<p>This document is large. Select Source to read its exact text, or download SKILL.md.</p>:<div className="skills-markdown"><Markdown skipHtml components={{img:({alt})=><span className="skills-warning">[Image: {alt||'attachment'} — available in package files]</span>,a:({href,children})=>href?<button className="skills-link" onClick={()=>void follow(href)}>{children}</button>:<span>{children}</span>}}>{text.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/,'')}</Markdown></div>}
    <details className="skills-files" open><summary>Package files ({detail.files.length})</summary><ul>{detail.files.map(file=><li key={file.path}><button className="skills-file-name" disabled={busy} onClick={()=>void inspect(file.path)}><FileText size={15}/>{file.path}</button><small>{file.size.toLocaleString()} bytes</small><button className="btn" aria-label={'Download '+file.path} disabled={busy} onClick={()=>void action(async()=>{const result=await api.download(detail.id,file.path,detail.contentRevision);if(result.saved)setNotice('File saved.');})}><Download size={15}/><span>Download</span></button></li>)}</ul></details>
    {preview&&<section className="skills-file-preview"><h3>{preview.path}</h3><button className="btn" onClick={()=>setPreview(null)}>Close preview</button><pre tabIndex={0}>{preview.text}</pre></section>}
    <footer className="skills-footer">{detail.trashed?<button className="btn" disabled={busy} onClick={()=>void action(async()=>{await api.restore({id:detail.id,expectedRevision:detail.revision,requestId:crypto.randomUUID()});setRevision(undefined);setRefresh(n=>n+1);},'Skill restored.')}>Restore skill</button>:confirmTrash?<><span>Move this skill to trash? Retained for 30 days.</span><button className="btn danger" disabled={busy} onClick={()=>void action(async()=>{await api.trash({id:detail.id,expectedRevision:detail.revision,requestId:crypto.randomUUID()});setRevision(undefined);setRefresh(n=>n+1);},'Skill moved to trash.')}>Confirm move to trash</button><button className="btn" onClick={()=>setConfirmTrash(false)}>Cancel</button></>:<button className="btn" disabled={busy} onClick={()=>setConfirmTrash(true)}>Move to trash</button>}</footer>
   </>}
  </article></div>
 </section>;
}
