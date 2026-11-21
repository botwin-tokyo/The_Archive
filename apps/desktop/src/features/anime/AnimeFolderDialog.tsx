import {useEffect,useState} from 'react';
import {ArrowLeft,Folder,LoaderCircle} from 'lucide-react';
import type {BackendPorts,Drive,Entry,Location} from '../../contracts';
import {archiveNameLabel,archivePathLabel} from '../../shared/lib/archiveLabels';
import {Dialog} from '../../shared/ui';

const message=(e:unknown)=>e instanceof Error?e.message:'Folders could not be listed. Try again.';

/** Existing File Storage folders only: browse, page beyond the first listing, never type a host path. */
export function AnimeFolderDialog({ports,start,onChoose,onClose}:{ports:BackendPorts;start:Location;onChoose:(location:Location)=>void;onClose:()=>void}) {
 const [location,setLocation]=useState<Location>(start);
 const [folders,setFolders]=useState<Entry[]>([]),[cursor,setCursor]=useState<string|null>(null);
 const [loading,setLoading]=useState(true),[error,setError]=useState('');
 const [drives,setDrives]=useState<Drive[]>([]);
 useEffect(()=>{let active=true;ports.drives.list().then(list=>{if(active)setDrives(list.filter(d=>d.role==='files'&&d.capabilities.write));}).catch(()=>{});return()=>{active=false;};},[ports]);
 const driveLabel=drives.find(d=>d.id===location.driveId)?.label??'Storage';
 useEffect(()=>{let active=true;setLoading(true);setFolders([]);setCursor(null);setError('');
  ports.files.list({location,requestId:crypto.randomUUID(),limit:500,foldersFirst:true})
   .then(page=>{if(active){setFolders(page.entries.filter(e=>e.kind==='directory'));setCursor(page.nextCursor??null);}})
   .catch(e=>{if(active)setError(message(e));}).finally(()=>{if(active)setLoading(false);});
  return()=>{active=false;};},[ports,location]);
 async function more(){if(!cursor)return;setLoading(true);try{const page=await ports.files.list({location,requestId:crypto.randomUUID(),limit:500,foldersFirst:true,cursor});setFolders(old=>[...old,...page.entries.filter(e=>e.kind==='directory')]);setCursor(page.nextCursor??null);}catch(e){setError(message(e));}finally{setLoading(false);}}
 const crumbs=location.path?location.path.split('/'):[];
 return <Dialog open title="Choose a destination folder" description="Episodes are saved inside a series folder created here." onClose={onClose}
  footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={loading&&!folders.length&&!error} onClick={()=>onChoose(location)}>Use this folder</button></>}>
  {drives.length>1&&<div className="anime-drives" role="group" aria-label="Drive">{drives.map(d=><button key={d.id} aria-pressed={d.id===location.driveId} onClick={()=>setLocation({driveId:d.id,path:''})}>{d.label}</button>)}</div>}
  <nav className="anime-crumbs" aria-label="Folder path">
   <button className="btn" disabled={!location.path} onClick={()=>setLocation({...location,path:crumbs.slice(0,-1).join('/')})}><ArrowLeft size={15}/> Up</button>
   <button className="anime-crumb" onClick={()=>setLocation({...location,path:''})}>{driveLabel}</button>
   {crumbs.map((part,i)=><span key={i}>/<button className="anime-crumb" aria-current={i===crumbs.length-1?'location':undefined} onClick={()=>setLocation({...location,path:crumbs.slice(0,i+1).join('/')})}>{archiveNameLabel(part)}</button></span>)}
  </nav>
  {error&&<p role="alert" className="anime-inline-error">{error}</p>}
  <div className="anime-folders" aria-label={`Folders in ${driveLabel}${location.path?' / '+archivePathLabel(location.path):''}`}>
   {folders.map(f=><button key={f.id} onClick={()=>setLocation({driveId:f.driveId,path:f.path})}><Folder size={17}/>{archiveNameLabel(f.name)}</button>)}
   {!loading&&!folders.length&&!error&&<p className="anime-muted">No subfolders here. You can use this folder.</p>}
   {loading&&<p role="status" className="anime-muted"><LoaderCircle size={14} className="anime-spin"/> Loading folders…</p>}
   {cursor&&!loading&&<button className="btn" onClick={()=>void more()}>More folders</button>}
  </div>
 </Dialog>;
}
