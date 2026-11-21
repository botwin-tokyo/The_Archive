import type {AnimeCatalogItem,AnimeJob,AnimePlan,AnimePort,AnimeSearch,AnimeSeries} from '../../contracts';
import {FILES_DRIVE_ID} from '../../fixtures/drives';
// Demo data only: nothing is downloaded and no file is created. Jobs stay paused with an explicit note.
const CATALOG:AnimeCatalogItem[]=[
 {anilistId:900001,malId:900001,title:'Archive Lights',altTitles:['Ākaibu no Hikari'],year:2024,format:'TV',status:'FINISHED',episodes:12,hasCover:false},
 {anilistId:900002,malId:null,title:'Archive Lights: Second Light',altTitles:[],year:2025,format:'TV',status:'RELEASING',episodes:null,hasCover:false},
 {anilistId:900003,malId:900003,title:'Archive Lights the Movie',altTitles:[],year:2026,format:'MOVIE',status:'FINISHED',episodes:1,hasCover:false},
];
const DEMO='Demo only — no files are created';
export function createMockAnime():AnimePort {
 const searches=new Map<string,AnimeSearch>(),series=new Map<string,AnimeSeries>(),plans=new Map<string,AnimePlan>(),jobs=new Map<string,AnimeJob>();
 const find=<T,>(map:Map<string,T>,id:string)=>{const value=map.get(id);if(!value)throw new Error('Anime request was not found');return value;};
 return {
  status:async()=>({enabled:true,available:true,provider:'ani.pm · simulated',catalog:'AniList · simulated',message:'Demo preview · no media is downloaded',defaultDestination:{driveId:FILES_DRIVE_ID,path:''},maxEpisodesPerJob:200,audio:['sub','dub'],mcp:false}),
  search:async args=>{const needle=args.query.trim().toLowerCase();const result:AnimeSearch={id:args.requestId,state:'completed',query:args.query.trim(),page:args.page??1,items:CATALOG.filter(c=>c.title.toLowerCase().includes(needle)||needle.includes('archive')),hasNextPage:false,error:null};searches.set(result.id,result);return result;},
  getSearch:async id=>find(searches,id),
  lookup:async args=>{
   const catalog=CATALOG.find(c=>c.anilistId===args.anilistId)??null;
   const candidates=[{providerSeriesId:'demo/archive-lights-1',title:'Archive Lights',year:2024,type:'TV',episodeCount:12,anilistId:900001,malId:900001,evidence:'anilist-id' as const},
    {providerSeriesId:'demo/archive-lights-second-light-2',title:'Archive Lights Second Light',year:2025,type:'TV',episodeCount:6,anilistId:null,malId:null,evidence:'title' as const}];
   // The sequel has no identity evidence, demonstrating an explicit owner choice.
   const chosen=args.providerSeriesId??(args.anilistId===900001?'demo/archive-lights-1':null);
   const ambiguous=args.anilistId===900002&&!args.providerSeriesId;
   const episodes=chosen?Array.from({length:chosen.endsWith('-1')?12:6},(_,i)=>({id:`demo.${chosen.endsWith('-1')?1:2}.${i+1}`,number:String(i+1),title:i===0?'First Light':null,sub:true,dub:i!==6})).concat(chosen.endsWith('-1')?[{id:'demo.recap',number:'6.5',title:'Recap',sub:true,dub:false}]:[]):[];
   const value:AnimeSeries={id:args.requestId,revision:1,state:'completed',anilistId:args.anilistId,requestedProvider:args.providerSeriesId??null,catalog,description:'A demonstration series used to explore the Anime page safely.',genres:['Drama','Slice of Life'],
    relations:args.anilistId===900001?[{anilistId:900002,title:'Archive Lights: Second Light',relation:'SEQUEL',format:'TV',year:2025}]:[],candidates:args.anilistId===900003?[]:candidates,
    match:{state:chosen?(args.providerSeriesId?'selected':'matched'):ambiguous?'ambiguous':'none',providerSeriesId:chosen,evidence:chosen?(args.providerSeriesId?'title':'anilist-id'):null},
    episodes,samples:chosen?{sub:{heights:[1080,720],subtitles:['en','es']},dub:{heights:[1080],subtitles:['en']}}:{sub:null,dub:null},expiresAt:Date.now()/1000+1800,error:null};
   series.set(value.id,value);return value;},
  getSeries:async id=>find(series,id),
  cover:async()=>({dataUrl:null}),
  resolve:async args=>{
   const source=find(series,args.seriesId);if(source.revision!==args.seriesRevision)throw new Error('The series details changed; reload them');
   const title=source.catalog?.title??'Untitled';const folder=[args.destination.path,`${title.replaceAll(':','_')} [AL-${source.anilistId}]`].filter(Boolean).join('/');
   const episodes=source.episodes.filter(e=>args.episodeIds.includes(e.id)).map(e=>{const name=`Episode ${e.number.split('.')[0].padStart(3,'0')}${e.number.includes('.')?'.'+e.number.split('.')[1]:''} [${args.audio}].mp4`;
    const status=!e[args.audio]?'unavailable' as const:e.number==='3'?'conflict' as const:'new' as const;
    return {id:e.id,number:e.number,title:e.title,name,path:`${folder}/${name}`,status,reason:status==='unavailable'?`The provider does not list a ${args.audio} release`:status==='conflict'?'An unknown file already exists at this name; it will not be overwritten':null,entry:null};});
   const count=(s:string)=>episodes.filter(e=>e.status===s).length;
   const plan:AnimePlan={id:args.requestId,revision:1,state:'ready',seriesId:source.id,anilistId:source.anilistId,title,providerSeriesId:source.match.providerSeriesId!,audio:args.audio,quality:args.quality,subtitles:args.subtitles,destination:args.destination,folder,episodes,
    counts:{total:episodes.length,new:count('new'),'already-present':0,conflict:count('conflict'),unavailable:count('unavailable')},estimate:'Demo mode creates no files.',expiresAt:Date.now()/1000+900};
   plans.set(plan.id,plan);return plan;},
  getPlan:async id=>find(plans,id),
  enqueue:async args=>{
   const existing=jobs.get(args.requestId);if(existing)return existing;
   const plan=find(plans,args.planId);if(plan.revision!==args.planRevision)throw new Error('Review the download plan again');
   const episodes=plan.episodes.map(e=>({id:e.id,number:e.number,title:e.title,name:e.name,state:e.status==='new'?'paused' as const:e.status==='unavailable'?'unavailable' as const:'conflict' as const,stage:null,progress:null,attempt:0,error:e.status==='new'?DEMO:e.reason,entry:null,height:null,sizeBytes:null}));
   const job:AnimeJob={id:args.requestId,revision:1,state:'paused',title:plan.title,anilistId:plan.anilistId,providerSeriesId:plan.providerSeriesId,audio:plan.audio,quality:plan.quality,subtitles:plan.subtitles,destination:plan.destination,folder:plan.folder,createdAt:Date.now()/1000,episodes,
    summary:{total:episodes.length,completed:0,failed:episodes.filter(e=>e.state==='conflict').length,unavailable:episodes.filter(e=>e.state==='unavailable').length,canceled:0,pending:episodes.filter(e=>e.state==='paused').length}};
   jobs.set(job.id,job);return job;},
  reconcile:async requestId=>jobs.has(requestId)?{action:'enqueue',id:requestId,state:jobs.get(requestId)!.state}:null,
  listJobs:async()=>({items:[...jobs.values()].reverse(),cursor:null}),
  getJob:async id=>find(jobs,id),
  control:async args=>{
   const job=find(jobs,args.id);if(job.revision!==args.revision)throw new Error('The anime job changed; refresh and try again');
   const episodes=job.episodes.map(e=>args.action==='cancel'&&!['completed','already-present'].includes(e.state)?{...e,state:'canceled' as const,error:null}:e);
   const next:AnimeJob={...job,revision:job.revision+1,state:args.action==='cancel'?'canceled':'paused',episodes};jobs.set(next.id,next);return next;},
 };
}
