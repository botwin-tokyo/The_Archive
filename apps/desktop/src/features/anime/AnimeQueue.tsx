import {useState} from 'react';
import {ChevronDown,ChevronRight,FolderOpen} from 'lucide-react';
import type {AnimeJob,AnimeJobAction,AnimeJobEpisode,Location} from '../../contracts';

const STAGE:Record<string,string>={queued:'Queued',resolving:'Finding stream',downloading:'Downloading',verifying:'Verifying media',ready:'Verified · waiting to save',importing:'Saving to archive',
 completed:'Saved',['already-present']:'Already in library',paused:'Paused','waiting-for-drive':'Waiting for drive','blocked-auth':'Permission needed',conflict:'Name conflict',unavailable:'Unavailable',failed:'Failed',canceled:'Canceled'};
const JOB:Record<string,string>={queued:'Queued',running:'Downloading',paused:'Paused','waiting-for-drive':'Waiting for drive',completed:'Complete',partial:'Finished with issues',failed:'Failed',canceled:'Canceled'};

function progress(job:AnimeJob){
 // Successful completion is shown only when every required episode has a terminal successful outcome.
 const s=job.summary;if(!s||!s.total)return 0;
 const active=job.episodes.reduce((sum,e)=>sum+(['completed','already-present'].includes(e.state)?1:e.state==='importing'||e.state==='ready'?0.97:e.progress??0),0);
 return job.state==='completed'?1:Math.min(0.99,active/s.total);
}

function summary(job:AnimeJob){
 const s=job.summary;if(!s)return '';
 return [`${s.completed}/${s.total} saved`,s.pending?`${s.pending} pending`:null,s.failed?`${s.failed} need attention`:null,s.unavailable?`${s.unavailable} unavailable`:null,s.canceled?`${s.canceled} canceled`:null].filter(Boolean).join(' · ');
}

export function AnimeQueue({jobs,busy,hiddenCount,onControl,onOpen,onClear,onRestore,hasOlder,onOlder,onNewest}:{jobs:AnimeJob[];busy:boolean;hiddenCount:number;onControl:(job:AnimeJob,action:AnimeJobAction)=>void;
 onOpen:(location:Location)=>void;onClear:()=>void;onRestore:()=>void;hasOlder:boolean;onOlder:()=>void;onNewest:(()=>void)|null}) {
 const [open,setOpen]=useState<Set<string>>(new Set());
 const toggle=(id:string)=>setOpen(old=>{const next=new Set(old);if(next.has(id))next.delete(id);else next.add(id);return next;});
 return <aside className="anime-queue" aria-label="Anime downloads">
  <div className="anime-queue-head"><h2>Downloads <small>{jobs.length}</small></h2>
   <div><button className="anime-link" disabled={!jobs.some(j=>['completed','canceled'].includes(j.state))} onClick={onClear}>Clear finished</button>{hiddenCount>0&&<button className="anime-link" onClick={onRestore}>Show cleared ({hiddenCount})</button>}</div></div>
  {!jobs.length&&<p className="anime-muted">Queued series appear here and keep downloading when you leave this page or close the desktop.</p>}
  {jobs.map(job=>{const expanded=open.has(job.id);const failed=job.episodes.some(e=>['failed','conflict','unavailable','blocked-auth'].includes(e.state));const pending=job.episodes.some(e=>!['completed','already-present','failed','conflict','unavailable','blocked-auth','canceled'].includes(e.state));
   return <article key={job.id} className={`anime-job is-${job.state}`}>
    <button className="anime-job-title" aria-expanded={expanded} onClick={()=>toggle(job.id)}>{expanded?<ChevronDown size={15}/>:<ChevronRight size={15}/>}<span><strong>{job.title}</strong><small>{job.audio==='sub'?'Sub':'Dub'} · {job.quality==='best'?'Best':job.quality} · {job.episodes.length} episode{job.episodes.length===1?'':'s'}</small></span></button>
    <div className="anime-job-status"><span>{JOB[job.state]??job.state}</span><span>{summary(job)}</span></div>
    <progress max={1} value={progress(job)} aria-label={`Progress for ${job.title}`}/>
    <div className="anime-job-actions">
     {['queued','running','waiting-for-drive'].includes(job.state)&&<button disabled={busy} onClick={()=>onControl(job,'pause')}>Pause</button>}
     {job.state==='paused'&&<button disabled={busy} onClick={()=>onControl(job,'resume')}>Resume</button>}
     {failed&&!['canceled'].includes(job.state)&&<button disabled={busy} onClick={()=>onControl(job,'retry-failed')}>Retry failed</button>}
     {pending&&!['canceled','completed'].includes(job.state)&&<button disabled={busy} onClick={()=>onControl(job,'cancel')}>Cancel remaining</button>}
     {job.episodes.some(e=>e.entry)&&<button onClick={()=>onOpen({driveId:job.destination.driveId,path:job.folder})}><FolderOpen size={13}/> Open folder</button>}
    </div>
    {expanded&&<ol className="anime-job-episodes">{job.episodes.map(e=><EpisodeRow key={e.id} episode={e}/>)}</ol>}
   </article>;})}
  <div className="anime-job-actions">{onNewest&&<button onClick={onNewest}>Newest</button>}{hasOlder&&<button onClick={onOlder}>Older downloads</button>}</div>
 </aside>;
}

function EpisodeRow({episode:e}:{episode:AnimeJobEpisode}){
 const percent=e.progress!==null&&['downloading','verifying'].includes(e.state)?` ${Math.round(e.progress*100)}%`:'';
 return <li className={`is-${e.state}`}><span className="anime-ep-number">EP {e.number}</span><span>{STAGE[e.state]??e.state}{percent}{e.height?` · ${e.height}p`:''}</span>{e.error&&<small>{e.error}</small>}</li>;
}
