import {useEffect,useState,useRef} from 'react';
import {AppShell} from '../shell';
import {Dialog} from '../shared/ui';
import {TransferQueue,createJobStore} from '../features/transfers';
import {Inspector} from '../features/inspector';
import {DriveTree} from '../features/drives';
import {FilesView} from '../features/files';
import {TrashView} from '../features/trash';
import {OperationDialog,type OperationDraft} from '../features/operations';
import type {Drive,Entry,Location,OperationKind,OperationRequest,Job,ServiceStatus} from '../contracts';
import {INITIAL_LOCATION} from '../fixtures/drives';
import {createDependencies} from './createDependencies';
import {readLastLocation,saveLastLocation} from './locationPreference';
import {DemoTools} from './DemoTools';
import {RetryableRoute} from './RetryableRoute';
import {jobAffectsLocation} from './jobAffectsLocation';
const loadSkillLibrary=()=>import('../features/skill-library/SkillLibraryView').then(module=>({default:module.SkillLibraryView}));
const loadPasswordManager=()=>import('../features/password-manager/PasswordManagerView').then(module=>({default:module.PasswordManagerView}));
const loadGitBackup=()=>import('../features/gitBackup/GitBackupView').then(module=>({default:module.GitBackupView}));
const loadYoinks=()=>import('../features/yoinks').then(module=>({default:module.YoinksView}));
const loadAnime=()=>import('../features/anime').then(module=>({default:module.AnimeView}));
const loadSpotifyDL=()=>import('../features/music').then(module=>({default:module.SpotifyDLView}));
const loadConnections=()=>import('../features/connections').then(module=>({default:module.ConnectionsView}));
const loadSettings=()=>import('../features/settings').then(module=>({default:module.SettingsView}));
type Navigation={route:string;location?:Location};
export default function App(){
 const requests=useRef(new Map<string,OperationDraft>());
 const [ports]=useState(createDependencies);
 const [jobs]=useState(()=>createJobStore(ports.jobs,`archive.queue.hidden.v1.${ports.mode}.transfers`));
 const [route,setRoute]=useState('files');
 const [location,setLocation]=useState<Location>(()=>readLastLocation(ports.mode));
 const [drives,setDrives]=useState<Drive[]>([]);
 const [selection,setSelection]=useState<Entry[]>([]);
 const [draft,setDraft]=useState<OperationDraft|null>(null);
 const [refresh,setRefresh]=useState(0);
 const [driveRefresh,setDriveRefresh]=useState(0);
 const currentLocation=useRef(location);currentLocation.current=location;
 const [dirty,setDirty]=useState(false);
 const [pendingNavigation,setPendingNavigation]=useState<Navigation|null>(null);
 const [status,setStatus]=useState<ServiceStatus|null>(null);
 useEffect(()=>{const controller=new AbortController();const initial=readLastLocation(ports.mode);if(ports.mode==='service'&&initial.driveId==='unconfigured')return;ports.files.list({location:initial,requestId:'restore-location',limit:1},{signal:controller.signal}).catch(error=>{if(!controller.signal.aborted&&error?.detail?.code==='NOT_FOUND'){setLocation({driveId:initial.driveId,path:''});setNotice('The saved folder is unavailable. Returned to its drive root.');}});return()=>controller.abort();},[ports]);
 const [notice,setNotice]=useState(window.location.hash==='#renderer-recovered'?(ports.mode==='mock'?'The demo renderer restarted. Its synthetic session was reset; no independent backend is connected.':'The renderer restarted. Rechecking the independent service and durable jobs.'):ports.mode==='service'?'Service mode · configure the endpoint and owner credential in Electron main. No demo data is used.':'');
 useEffect(()=>{
  jobs.start();const timer=ports.mode==='mock'?setInterval(()=>ports.clock.advance(500),500):undefined;
  let refreshTimer:ReturnType<typeof setTimeout>|undefined,folderChanged=false;
  const schedule=(affectsFolder:boolean)=>{folderChanged ||= affectsFolder;if(refreshTimer!==undefined)return;refreshTimer=setTimeout(()=>{refreshTimer=undefined;if(folderChanged)setRefresh(value=>value+1);folderChanged=false;setDriveRefresh(value=>value+1);},250);};
  const unsubscribe=ports.jobs.subscribe(event=>{if(['completed','failed','canceled'].includes(event.job.state))schedule(jobAffectsLocation(event.job,currentLocation.current));},()=>schedule(true));
  return()=>{clearInterval(timer);clearTimeout(refreshTimer);unsubscribe();jobs.stop();};
 },[ports,jobs]);
 useEffect(()=>{let active=true;ports.drives.list().then(result=>{if(active){setDrives(result);if(ports.mode==='service'&&result.length)setLocation(current=>result.some(item=>item.id===current.driveId)?current:{driveId:result[0].id,path:''});}}).catch(()=>{if(active)setNotice('Drive information is unavailable. Cached identities remain visible.');});return()=>{active=false;};},[ports,refresh,driveRefresh,status?.backend]);
 useEffect(()=>{let active=true;ports.status.get().then(value=>{if(active)setStatus(value);});const unsubscribe=ports.status.subscribe(value=>{setStatus(value);void jobs.reconcile();});return()=>{active=false;unsubscribe();};},[ports,jobs]);
 function applyNavigation(next:Navigation){setRoute(next.route);if(next.location){setLocation(next.location);saveLastLocation(next.location,ports.mode);setSelection([]);}setPendingNavigation(null);}
 function requestNavigation(next:Navigation){if(['settings','password-manager'].includes(route)&&dirty&&next.route!==route)setPendingNavigation(next);else applyNavigation(next);}
 const navigate=(next:Location)=>requestNavigation({route:'files',location:next});
 const unavailable=status?.backend!=='connected'||Boolean(status?.stale);
 useEffect(()=>{if(ports.mode!=='service')return;void window.archiveDesktop?.getVersion().then(info=>{if(info.configured)setNotice('Service mode · connected storage changes are real. Review targets before submitting.');}).catch(()=>setNotice('Desktop configuration is unavailable. Mutations remain disabled.'));},[ports]);
 const operation=(kind:OperationKind,entries:Entry[])=>{if(unavailable){setNotice('The backend is unavailable. Reconnect before changing data.');return;}setDraft({kind,entries:[...entries],location:{...location}});};
 const accepted=(request:OperationRequest)=>{if(draft){requests.current.set(request.requestId,{...draft,request});if(requests.current.size>1000)requests.current.delete(requests.current.keys().next().value!);}void jobs.reconcile();};
 const resolve=(job:Job)=>{const previous=requests.current.get(job.requestId);if(previous)setDraft(previous);else setNotice('The originating draft is unavailable in this session. Review the durable job before making another change.');};
 const selectedDrive=drives.find(drive=>drive.id===location.driveId);
 return <><AppShell mode={ports.mode} status={status} route={route} onRoute={next=>requestNavigation({route:next})}
 sidebar={<DriveTree drives={drives} files={ports.files} location={location} onNavigate={navigate} onTrash={id=>requestNavigation({route:'trash',location:{driveId:id,path:''}})}/>}
 inspector={<Inspector port={ports.files} selection={selection} location={location} drive={selectedDrive} onOperation={unavailable?undefined:operation}/>}
 queue={<TransferQueue simulated={ports.mode==='mock'} store={jobs} onResolve={resolve}/> }>
 {import.meta.env.DEV&&ports.mode==='mock'&&<DemoTools backend={ports} onReset={()=>{requests.current.clear();jobs.reset();setSelection([]);setLocation(INITIAL_LOCATION);setRefresh(value=>value+1);}}/>}
 {notice&&<div className="notice notice--warning" role="status">{notice}<button onClick={()=>setNotice('')} aria-label="Dismiss notice">×</button></div>}
 {status?.stale&&<div className="notice notice--warning" role="status">Cached {ports.mode==='mock'?'demo ':''}data · backend unavailable. Mutations disabled.</div>}
 {route==='files'?<FilesView port={ports.files} location={location} onNavigate={navigate} onSelection={setSelection} drive={selectedDrive} onOperation={unavailable?undefined:operation} refreshKey={refresh}/>
 :route==='trash'&&selectedDrive?<TrashView port={ports.trash} drive={selectedDrive} drives={drives} onDrive={id=>setLocation({driveId:id,path:''})} onOperation={operation} refreshKey={refresh}/>
 :route==='skill-library'?<RetryableRoute load={loadSkillLibrary} name="Skill Library" routeProps={{ports}}/>
 :route==='password-manager'?<RetryableRoute load={loadPasswordManager} name="Password Manager" routeProps={{ports,onDirtyChange:setDirty}}/>
 :route==='transfers'?<TransferQueue simulated={ports.mode==='mock'} store={jobs} full onResolve={resolve}/>
 :route==='github'?<RetryableRoute load={loadGitBackup} name="GitHub Backup" routeProps={{ports,onNavigate:navigate}}/>
 :route==='yoinks'?<RetryableRoute load={loadYoinks} name="Yoinks" routeProps={{ports,onNavigate:navigate}}/>
 :route==='anime'?<RetryableRoute load={loadAnime} name="Anime" routeProps={{ports,onNavigate:navigate}}/>
 :route==='spotifydl'?<RetryableRoute load={loadSpotifyDL} name="SpotifyDL" routeProps={{ports,onNavigate:navigate}}/>
 :route==='connections'?<RetryableRoute load={loadConnections} name="Connections" routeProps={{ports}}/>
 :route==='settings'?<RetryableRoute load={loadSettings} name="Settings" routeProps={{ports,onDirtyChange:setDirty,onSaved:()=>setRefresh(value=>value+1)}}/>
 :<div className="empty-state">Choose a drive or section</div>}
 </AppShell>
 {draft&&<OperationDialog draft={draft} ports={ports} drives={drives} onClose={()=>setDraft(null)} onAccepted={accepted}/>}
 <Dialog open={Boolean(pendingNavigation)} title="Discard unsaved changes?" description="Your draft has not been saved. Transfers continue independently." onClose={()=>setPendingNavigation(null)} footer={<><button className="btn" onClick={()=>setPendingNavigation(null)}>Keep editing</button><button className="btn danger" onClick={()=>{setDirty(false);if(pendingNavigation)applyNavigation(pendingNavigation);}}>Discard draft and leave</button></>}><p>Leave this page only if you want to discard the current settings draft.</p></Dialog>
 </>;
}
