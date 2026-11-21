import {useEffect,useMemo,useRef,useState} from 'react';
import {AlertTriangle,ArrowLeft,Check,Link2,ListChecks} from 'lucide-react';
import type {AnimeAudio,AnimeSeries,AnimeSubtitleLanguage,BackendPorts} from '../../contracts';
import {AnimeCover,formatLine} from './AnimeResults';

export const LANGUAGE:Record<string,string>={en:'English',es:'Spanish',pt:'Portuguese',fr:'French',de:'German',it:'Italian',ru:'Russian',ar:'Arabic',ja:'Japanese',zh:'Chinese',ko:'Korean',id:'Indonesian',th:'Thai',vi:'Vietnamese',pl:'Polish',tr:'Turkish',hi:'Hindi',ms:'Malay'};
const RELATION:Record<string,string>={SEQUEL:'Sequel',PREQUEL:'Prequel',SIDE_STORY:'Side story',SPIN_OFF:'Spin-off',ALTERNATIVE:'Alternative',PARENT:'Parent story',SUMMARY:'Summary',COMPILATION:'Compilation',CONTAINS:'Contains',CHARACTER:'Shared characters',ADAPTATION:'Adaptation',SOURCE:'Source',OTHER:'Related'};
const EVIDENCE:Record<string,string>={'anilist-id':'AniList ID match','mal-id':'MyAnimeList ID match',title:'Title match only'};
const MATCHED_BY:Record<string,string>={'anilist-id':'AniList ID','mal-id':'MyAnimeList ID',title:'title'};
const PAGE=100;
export const MAX_BATCH=200;

export interface AnimeChoice {episodeIds:string[];audio:AnimeAudio;quality:string;subtitles:AnimeSubtitleLanguage[]}

export function AnimeDetails({ports,series,busy,onBack,onChooseProvider,onOpenRelation,onReview}:{ports:BackendPorts;series:AnimeSeries;busy:boolean;onBack:()=>void;
 onChooseProvider:(providerSeriesId:string)=>void;onOpenRelation:(anilistId:number)=>void;onReview:(choice:AnimeChoice)=>void}) {
 const heading=useRef<HTMLHeadingElement>(null);
 const audios=(['sub','dub'] as const).filter(a=>series.samples[a]);
 const [audio,setAudio]=useState<AnimeAudio>(audios[0]??'sub');
 const sample=series.samples[audio];
 const [quality,setQuality]=useState('best'),[subtitles,setSubtitles]=useState<AnimeSubtitleLanguage[]>([]);
 const [selected,setSelected]=useState<Set<string>>(new Set()),[shown,setShown]=useState(PAGE),[notice,setNotice]=useState('');
 const [from,setFrom]=useState(''),[to,setTo]=useState(''),[correcting,setCorrecting]=useState(false),[expanded,setExpanded]=useState(false);
 const available=useMemo(()=>series.episodes.filter(e=>e[audio]),[series.episodes,audio]);
 useEffect(()=>{heading.current?.focus();},[series.id]);
 useEffect(()=>{setSelected(new Set());setShown(PAGE);setCorrecting(false);setNotice('');},[series.id]);
 // Defaults follow the provider's real offer: the tallest exact height and English subtitles for sub.
 useEffect(()=>{
  setQuality(sample?`${sample.heights[0]}p`:'best');
  setSubtitles(sample&&audio==='sub'&&sample.subtitles.includes('en')?['en']:[]);
  setSelected(old=>{const keep=new Set([...old].filter(id=>series.episodes.find(e=>e.id===id)?.[audio]));
   if(keep.size<old.size)setNotice(`${old.size-keep.size} selected episode${old.size-keep.size===1?' has':'s have'} no ${audio} release and ${old.size-keep.size===1?'was':'were'} deselected.`);return keep;});
 },[audio,series.id]); // Re-run only when the audio track or series changes, not on each sample object.
 const toggle=(id:string)=>setSelected(old=>{const next=new Set(old);if(next.has(id))next.delete(id);else next.add(id);return next;});
 function selectRange(){
  const low=Number(from),high=Number(to||from);
  if(!from||!Number.isFinite(low)||!Number.isFinite(high)||high<low){setNotice('Enter a valid episode range, for example 1 to 12.');return;}
  const ids=available.filter(e=>Number(e.number)>=low&&Number(e.number)<=high).map(e=>e.id);
  setSelected(old=>new Set([...old,...ids]));setNotice(ids.length?`Added ${ids.length} available episode${ids.length===1?'':'s'} from ${low} to ${high}.`:`No ${audio} episodes are available from ${low} to ${high}.`);
 }
 const catalog=series.catalog;
 const match=series.match;
 const over=selected.size>MAX_BATCH;
 return <article className="anime-details" aria-labelledby={`series-${series.id}`}>
  <button className="anime-back" onClick={onBack}><ArrowLeft size={15}/> Back to results</button>
  <header className="anime-hero">
   {catalog&&<AnimeCover ports={ports} item={catalog} size="hero"/>}
   <div>
    <span className="eyebrow">ANILIST {series.anilistId}</span>
    <h2 id={`series-${series.id}`} ref={heading} tabIndex={-1}>{catalog?.title??'Series'}</h2>
    {catalog&&catalog.altTitles.length>0&&<p className="anime-alt">{catalog.altTitles.slice(0,3).join(' · ')}</p>}
    {catalog&&<p className="anime-meta">{formatLine(catalog)}{catalog.status?` · ${catalog.status.toLowerCase().replaceAll('_',' ')}`:''}</p>}
    {series.genres.length>0&&<div className="anime-tags">{series.genres.map(g=><span key={g}>{g}</span>)}</div>}
    {series.description&&<p className={`anime-synopsis ${expanded?'is-open':''}`}>{series.description}</p>}
    {series.description&&series.description.length>320&&<button className="anime-link" onClick={()=>setExpanded(v=>!v)}>{expanded?'Show less':'Read more'}</button>}
   </div>
  </header>

  <section className="anime-match" aria-label="Provider match">
   {match.state==='matched'||match.state==='selected'?<div className="anime-match-line"><Link2 size={16}/><div>
     <strong>{match.state==='selected'?'Using your selected provider series':`Matched by ${MATCHED_BY[match.evidence??'title']}`}</strong>
     <small>{series.candidates.find(c=>c.providerSeriesId===match.providerSeriesId)?.title??match.providerSeriesId} · {series.episodes.length} provider episode{series.episodes.length===1?'':'s'}</small></div>
     {series.candidates.length>1&&<button className="anime-link" onClick={()=>setCorrecting(v=>!v)}>{correcting?'Keep this match':'Wrong series?'}</button>}</div>
   :<div className="notice notice--warning" role="status"><AlertTriangle size={16}/> {match.state==='ambiguous'?'Several provider series could match. Choose the correct one before downloading.':
     series.candidates.length?'No provider series has a matching identity. Choose one only if you are sure it is this series.':'This series is not available from the provider. Catalog details are still shown.'}</div>}
   {(correcting||match.state==='ambiguous'||(match.state==='none'&&series.candidates.length>0))&&<ul className="anime-candidates" aria-label="Provider series candidates">
    {series.candidates.map(c=><li key={c.providerSeriesId}><div><strong>{c.title}</strong><small>{[c.year,c.type,c.episodeCount!==null?`${c.episodeCount} eps`:null,EVIDENCE[c.evidence]].filter(Boolean).join(' · ')}</small></div>
     {c.providerSeriesId===match.providerSeriesId?<span className="anime-chip"><Check size={13}/> Current</span>:<button className="btn" disabled={busy} onClick={()=>onChooseProvider(c.providerSeriesId)}>Use this series</button>}</li>)}
   </ul>}
  </section>

  {series.relations.length>0&&<section className="anime-relations" aria-label="Related entries"><span className="eyebrow">RELATED · SEPARATE ENTRIES</span>
   <div>{series.relations.map(r=><button key={r.anilistId+r.relation} onClick={()=>onOpenRelation(r.anilistId)}><small>{RELATION[r.relation]??'Related'}</small>{r.title}{r.year?` (${r.year})`:''}</button>)}</div></section>}

  {match.providerSeriesId&&<section className="anime-episodes" aria-label="Episodes">
   <div className="anime-options">
    <fieldset><legend>Audio</legend>{(['sub','dub'] as const).map(a=><button key={a} aria-pressed={audio===a} disabled={!series.samples[a]} onClick={()=>setAudio(a)}>{a==='sub'?'Japanese · sub':'English · dub'}{!series.samples[a]&&<small> unavailable</small>}</button>)}</fieldset>
    {sample&&<fieldset><legend>Quality</legend>{[...sample.heights.map(h=>`${h}p`),'best'].map(q=><button key={q} aria-pressed={quality===q} onClick={()=>setQuality(q)}>{q==='best'?'Best available':q}</button>)}</fieldset>}
    {sample&&<fieldset><legend>Subtitles</legend>{sample.subtitles.length?sample.subtitles.map(s=><button key={s} aria-pressed={subtitles.includes(s)} onClick={()=>setSubtitles(old=>old.includes(s)?old.filter(x=>x!==s):[...old,s])}>{LANGUAGE[s]??s}</button>):<span className="anime-muted">None offered</span>}</fieldset>}
   </div>
   {!audios.length&&<p className="notice" role="status">The provider did not offer a playable sample for this series. Episodes cannot be queued right now.</p>}
   <div className="anime-selection-bar">
    <span role="status" aria-live="polite"><ListChecks size={15}/> {selected.size} of {available.length} available {audio} episode{available.length===1?'':'s'} selected</span>
    <button className="anime-link" disabled={!available.length} onClick={()=>setSelected(new Set(available.map(e=>e.id)))}>Select all {available.length} available</button>
    <button className="anime-link" disabled={!selected.size} onClick={()=>setSelected(new Set())}>Clear</button>
    <form className="anime-range" onSubmit={e=>{e.preventDefault();selectRange();}}><label>From <input className="anime-range-input" inputMode="decimal" value={from} onChange={e=>setFrom(e.target.value)} aria-label="Range start episode"/></label><label>to <input className="anime-range-input" inputMode="decimal" value={to} onChange={e=>setTo(e.target.value)} aria-label="Range end episode"/></label><button className="btn">Add range</button></form>
   </div>
   {notice&&<p className="anime-notice" role="status">{notice}</p>}
   <ol className="anime-episode-list">
    {series.episodes.slice(0,shown).map(e=>{const ok=e[audio];return <li key={e.id} className={ok?'':'is-unavailable'}>
     <label><input type="checkbox" aria-label={`Episode ${e.number}${e.title?`: ${e.title}`:''}${ok?'':` (no ${audio} release)`}`} checked={selected.has(e.id)} disabled={!ok} onChange={()=>toggle(e.id)}/><span className="anime-ep-number">EP {e.number}</span><span className="anime-ep-title">{e.title??`Episode ${e.number}`}</span>
      <span className="anime-ep-flags">{e.sub&&<small>SUB</small>}{e.dub&&<small>DUB</small>}{!ok&&<em>No {audio} release</em>}</span></label></li>;})}
   </ol>
   {shown<series.episodes.length&&<button className="btn anime-more" onClick={()=>setShown(n=>n+PAGE)}>Show {Math.min(PAGE,series.episodes.length-shown)} more of {series.episodes.length-shown} remaining</button>}
   <div className="anime-review-bar">
    {over&&<p role="alert" className="anime-inline-error">Select at most {MAX_BATCH} episodes per download; queue longer series in batches.</p>}
    <button className="btn primary" disabled={busy||!selected.size||over||!sample} onClick={()=>onReview({episodeIds:series.episodes.filter(e=>selected.has(e.id)).map(e=>e.id),audio,quality,subtitles:[...subtitles].sort((a,b)=>Object.keys(LANGUAGE).indexOf(a)-Object.keys(LANGUAGE).indexOf(b))})}>Review {selected.size||''} episode{selected.size===1?'':'s'}</button>
   </div>
  </section>}
 </article>;
}
