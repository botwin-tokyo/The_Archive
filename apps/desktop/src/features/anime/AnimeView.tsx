import {useEffect,useRef,useState} from 'react';
import {ArrowUpRight,Folder,LoaderCircle,Search,ShieldCheck,Tv} from 'lucide-react';
import type {AnimeCatalogItem,AnimeJob,AnimeJobAction,AnimePlan,AnimeSearch,AnimeSeries,AnimeStatus,BackendPorts,Location} from '../../contracts';
import {locationSchema} from '../../contracts';
import {archivePathLabel} from '../../shared/lib/archiveLabels';
import {useQueueHistory} from '../../shared/lib/queueHistory';
import {AnimeDetails,type AnimeChoice} from './AnimeDetails';
import {AnimeFolderDialog} from './AnimeFolderDialog';
import {AnimeQueue} from './AnimeQueue';
import {AnimeResults} from './AnimeResults';
import {AnimeReviewDialog} from './AnimeReviewDialog';
import './anime.css';

const message=(e:unknown)=>e instanceof Error?e.message:'The request could not complete. Try again.';
const pending=(state:string)=>state==='queued'||state==='running';
const DEBOUNCE_MS=450;

function readDestination(key:string):Location|null{try{const parsed=locationSchema.safeParse(JSON.parse(localStorage.getItem(key)??'null'));return parsed.success?parsed.data:null;}catch{return null;}}
function saveDestination(key:string,value:Location){try{localStorage.setItem(key,JSON.stringify(value));}catch{/* The page works without optional presentation storage. */}}

export function AnimeView({ports,onNavigate}:{ports:BackendPorts;onNavigate:(location:Location)=>void}) {
 const history=useQueueHistory(`archive.queue.hidden.v1.${ports.mode}.anime`);
 const destinationKey=`archive.anime.destination.v1.${ports.mode}`;
 const [status,setStatus]=useState<AnimeStatus|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [query,setQuery]=useState(''),[search,setSearch]=useState<AnimeSearch|null>(null),[results,setResults]=useState<AnimeCatalogItem[]>([]);
 const [series,setSeries]=useState<AnimeSeries|null>(null);
 const [destination,setDestination]=useState<Location|null>(()=>readDestination(destinationKey)),[browsing,setBrowsing]=useState(false);
 const [plan,setPlan]=useState<AnimePlan|null>(null),[reviewError,setReviewError]=useState(''),[pendingChoice,setPendingChoice]=useState<AnimeChoice|null>(null);
 const [driveLabels,setDriveLabels]=useState<Record<string,string>>({});
 useEffect(()=>{let active=true;ports.drives.list().then(list=>{if(active)setDriveLabels(Object.fromEntries(list.map(d=>[d.id,d.label])));}).catch(()=>{});return()=>{active=false;};},[ports]);
 const placeLabel=(l:Location)=>[driveLabels[l.driveId]??l.driveId,archivePathLabel(l.path)].filter(Boolean).join(' / ');
 const [jobs,setJobs]=useState<AnimeJob[]>([]),[jobPage,setJobPage]=useState<string|null>(null),[jobCursor,setJobCursor]=useState<string|null>(null),[notice,setNotice]=useState('');
 const lastSearched=useRef(''),searchGeneration=useRef(0),seriesGeneration=useRef(0),enqueueIds=useRef(new Map<string,string>()),lock=useRef(false);
 const searchRef=useRef(search),seriesRef=useRef(series);searchRef.current=search;seriesRef.current=series;

 // Durable state is polled from the service; leaving the page never cancels work.
 useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;
  const poll=async()=>{try{
   const [next,list]=await Promise.all([ports.anime.status(),ports.anime.listJobs(jobPage?{cursor:jobPage,limit:20}:{limit:20})]);if(!active)return;
   setStatus(next);setJobs(list.items);setJobCursor(list.cursor);
   const s=searchRef.current;if(s&&pending(s.state)){const value=await ports.anime.getSearch(s.id);if(active&&searchRef.current?.id===s.id)applySearch(value);}
   const d=seriesRef.current;if(d&&pending(d.state)){const value=await ports.anime.getSeries(d.id);if(active&&seriesRef.current?.id===d.id)setSeries(value);}
  }catch(e){if(active){setError(message(e));setStatus(s=>s?{...s,available:false}:s);}}finally{if(active)timer=setTimeout(()=>void poll(),2000);}};
  void poll();return()=>{active=false;clearTimeout(timer);};},[ports,jobPage]); // Pending search/series are read through refs, so polling does not restart on every update.
 useEffect(()=>{if(!destination&&status?.defaultDestination)setDestination(status.defaultDestination);},[status,destination]);

 function applySearch(value:AnimeSearch){
  setSearch(value);
  if(value.state==='completed')setResults(old=>value.page===1?value.items:[...old,...value.items.filter(i=>!old.some(o=>o.anilistId===i.anilistId))]);
 }
 async function runSearch(text:string,page=1){
  const current=++searchGeneration.current;if(page===1){lastSearched.current=text;setResults([]);setSeries(null);}
  try{const value=await ports.anime.search({requestId:crypto.randomUUID(),query:text,page});if(current===searchGeneration.current)applySearch(value);}
  catch(e){if(current===searchGeneration.current)setError(message(e));}
 }
 // Debounced typing; obsolete responses are discarded by generation.
 // Typing searches only when the worker is ready; Enter or the Search button always submits explicitly.
 useEffect(()=>{const text=query.trim();if(text.length<2||!status?.available)return;const timer=setTimeout(()=>{if(text!==lastSearched.current)void runSearch(text);},DEBOUNCE_MS);return()=>clearTimeout(timer);},[query]);
 async function perform(action:()=>Promise<void>){if(lock.current)return;lock.current=true;setBusy(true);setError('');try{await action();}catch(e){setError(message(e));}finally{lock.current=false;setBusy(false);}}
 async function openSeries(anilistId:number,providerSeriesId?:string){
  const current=++seriesGeneration.current;
  await perform(async()=>{const value=await ports.anime.lookup({requestId:crypto.randomUUID(),anilistId,...(providerSeriesId?{providerSeriesId}:{})});if(current===seriesGeneration.current)setSeries(value);});
 }
 async function review(choice:AnimeChoice){
  if(!series)return;
  if(!destination){setPendingChoice(choice);setBrowsing(true);return;}
  await perform(async()=>{setReviewError('');const next=await ports.anime.resolve({requestId:crypto.randomUUID(),seriesId:series.id,seriesRevision:series.revision,destination,...choice});setPlan(next);});
 }
 async function enqueue(){
  if(!plan)return;const requestId=enqueueIds.current.get(plan.id)??crypto.randomUUID();enqueueIds.current.set(plan.id,requestId);
  setBusy(true);setReviewError('');
  try{const job=await ports.anime.enqueue({requestId,planId:plan.id,planRevision:plan.revision});finish(job.id);}
  catch(e){
   // An uncertain submission is reconciled by its stable request ID instead of being sent as a new request.
   try{const prior=await ports.anime.reconcile(requestId);if(prior?.action==='enqueue'){finish(prior.id);return;}}catch{/* fall through to the original error */}
   setReviewError(message(e));
  }finally{setBusy(false);}
 }
 function finish(jobId:string){setNotice(`Queued ${plan?.counts.new??''} episode${plan?.counts.new===1?'':'s'} of ${plan?.title}. Downloads continue in the background.`);setPlan(null);setJobPage(null);void ports.anime.getJob(jobId).then(job=>setJobs(old=>[job,...old.filter(j=>j.id!==job.id)])).catch(()=>{});}
 async function control(job:AnimeJob,action:AnimeJobAction){await perform(async()=>{const fresh=await ports.anime.getJob(job.id);const next=await ports.anime.control({id:job.id,revision:fresh.revision,action});setJobs(old=>old.map(j=>j.id===next.id?next:j));});}
 function chooseDestination(location:Location){setDestination(location);saveDestination(destinationKey,location);setBrowsing(false);if(pendingChoice){const choice=pendingChoice;setPendingChoice(null);void reviewWith(choice,location);}}
 async function reviewWith(choice:AnimeChoice,location:Location){if(!series)return;await perform(async()=>{setPlan(await ports.anime.resolve({requestId:crypto.randomUUID(),seriesId:series.id,seriesRevision:series.revision,destination:location,...choice}));});}

 const visibleJobs=jobs.filter(j=>!history.hidden.has(j.id));
 const searching=search&&pending(search.state);
 const loadingSeries=series&&pending(series.state);
 return <section className="anime" aria-label="Anime">
  <header className="anime-heading"><div><span className="eyebrow">SERIES TO SHELF</span><h1>Anime<span>.</span></h1><p>Find a series, choose episodes and keep them in your archive.</p></div>
   <div className={`anime-ready ${status?.available?'is-ready':''}`}><span className="status-dot"/>{status?status.available?'Ready to download':'Worker unavailable':'Connecting…'}<small>{status?`${status.catalog} · ${status.provider}`:'AniList · ani.pm'}</small></div></header>
  {error&&<div role="alert" className="notice notice--warning">{error}<button aria-label="Dismiss anime error" onClick={()=>setError('')}>×</button></div>}
  {status&&!status.available&&<div className="notice" role="status">{status.message}</div>}
  {notice&&<div className="notice" role="status">{notice}<button aria-label="Dismiss notice" onClick={()=>setNotice('')}>×</button></div>}
  <form className="anime-search" role="search" onSubmit={e=>{e.preventDefault();if(query.trim())void runSearch(query.trim());}}>
   <Search size={20}/><input type="search" aria-label="Search anime" placeholder="Search a title, e.g. Frieren" maxLength={200} value={query} onChange={e=>setQuery(e.target.value)}/>
   {searching&&<LoaderCircle size={16} className="anime-spin" aria-label="Searching"/>}
   <button className="btn primary" disabled={!query.trim()||!status?.available}>Search</button>
  </form>
  <div className="anime-destination"><Folder size={18}/><div><span className="eyebrow">SAVE TO</span>{destination?<button className="anime-link" onClick={()=>onNavigate(destination)}>{placeLabel(destination)} <ArrowUpRight size={12}/></button>:<span>Choose a folder before reviewing</span>}</div>
   <span className="anime-format">MP4 · H.264/AAC · selectable subtitles</span><button className="btn" onClick={()=>setBrowsing(true)}>{destination?'Change folder':'Choose folder'}</button></div>
  <div className="anime-workspace">
   <div className="anime-main">
    {series?(loadingSeries?<div className="anime-empty" role="status"><LoaderCircle size={22} className="anime-spin"/><p>Loading series details and provider episodes…</p></div>
     :series.state==='failed'?<div className="anime-empty"><p role="alert">{series.error??'Series details are unavailable.'}</p><button className="btn" onClick={()=>void openSeries(series.anilistId)}>Try again</button><button className="btn" onClick={()=>setSeries(null)}>Back to results</button></div>
     :<AnimeDetails ports={ports} series={series} busy={busy} onBack={()=>setSeries(null)} onChooseProvider={id=>void openSeries(series.anilistId,id)} onOpenRelation={id=>void openSeries(id)} onReview={choice=>void review(choice)}/>)
    :search?<section aria-label="Search results" className="anime-search-results">
      <div className="anime-section-title"><h2>{searching?`Searching “${search.query}”…`:search.state==='failed'?'Search unavailable':`Results for “${search.query}”`}</h2><span>ANILIST CATALOG{search.cached?' · RECENT':''}</span></div>
      {search.state==='failed'&&<p role="alert" className="anime-inline-error">{search.error}</p>}
      {search.state==='completed'&&!results.length&&<div className="anime-empty"><p>No anime matched “{search.query}”. Try another spelling or the Japanese title.</p></div>}
      <AnimeResults ports={ports} items={results} onOpen={item=>void openSeries(item.anilistId)}/>
      {search.state==='completed'&&search.hasNextPage&&<button className="btn anime-more" onClick={()=>void runSearch(search.query,search.page+1)}>More results</button>}
     </section>
    :<div className="anime-empty anime-intro"><Tv size={54} strokeWidth={1.2}/><h2>Start with a title.</h2><p>Search the AniList catalog, check the provider’s real episode list, then choose exactly what to keep.</p><span><ShieldCheck size={14}/> Isolated downloads · verified before saving · never overwrites</span></div>}
   </div>
   <AnimeQueue jobs={visibleJobs} busy={busy} hiddenCount={history.hidden.size} onControl={(job,action)=>void control(job,action)} onOpen={onNavigate}
    onClear={()=>history.hide(jobs.filter(j=>['completed','canceled'].includes(j.state)).map(j=>j.id))} onRestore={history.restore} hasOlder={Boolean(jobCursor)} onOlder={()=>setJobPage(jobCursor)} onNewest={jobPage?()=>setJobPage(null):null}/>
  </div>
  <footer className="anime-footer"><span>Downloads continue when you leave this page or close the desktop.</span><span>Availability varies by series and episode · {status?.mcp?'MCP · owner access':'Demo'}</span></footer>
  {browsing&&<AnimeFolderDialog ports={ports} start={destination??status?.defaultDestination??{driveId:'files',path:''}} onChoose={chooseDestination} onClose={()=>{setBrowsing(false);setPendingChoice(null);}}/>}
  {plan&&<AnimeReviewDialog plan={plan} driveLabel={driveLabels[plan.destination.driveId]??plan.destination.driveId} busy={busy} error={reviewError} onConfirm={()=>void enqueue()} onClose={()=>setPlan(null)}/>}
 </section>;
}
