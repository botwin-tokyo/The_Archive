import {describe,expect,it} from 'vitest';
import {createServiceBackend,SERVICE_ACTIONS,type DesktopBridge} from '../../src/adapters/service/createServiceBackend';
import {createMockAnime} from '../../src/adapters/mock/anime';
const success=(result:unknown)=>({version:1,mode:'service',ok:true,result});
function bridgeWith(handler:(action:string,args:unknown)=>Promise<unknown>):DesktopBridge{return {mode:'service',getVersion:async()=>({schemaVersion:1,appVersion:'test',mode:'service',configured:true}),backend:Object.fromEntries(SERVICE_ACTIONS.map(action=>[action,(args:unknown)=>handler(action,args)])) as DesktopBridge['backend']};}

describe('anime service adapter',()=>{
 it('maps every anime port method to its named bridge action with validated results',async()=>{
  const mock=createMockAnime();const series=await mock.lookup({requestId:'s',anilistId:900001});
  const plan=await mock.resolve({requestId:'p',seriesId:series.id,seriesRevision:1,episodeIds:[series.episodes[0].id],audio:'sub',quality:'1080p',subtitles:['en'],destination:{driveId:'files',path:''}});
  const job=await mock.enqueue({requestId:'j',planId:plan.id,planRevision:1});
  const outputs:Record<string,unknown>={getAnimeStatus:await mock.status(),searchAnime:await mock.search({requestId:'q',query:'archive'}),getAnimeSearch:await mock.search({requestId:'q',query:'archive'}),lookupAnimeSeries:series,getAnimeSeries:series,
   getAnimeCover:{dataUrl:null},resolveAnime:plan,getAnimePlan:plan,enqueueAnime:job,reconcileAnime:null,listAnimeJobs:{items:[job],cursor:null},getAnimeJob:job,controlAnimeJob:job};
  const calls:string[]=[];const ports=createServiceBackend(bridgeWith(async action=>{calls.push(action);return success(outputs[action]);}));
  await ports.anime.status();await ports.anime.search({requestId:'q',query:'archive'});await ports.anime.getSearch('q');await ports.anime.lookup({requestId:'s',anilistId:900001});await ports.anime.getSeries('s');
  await ports.anime.cover(900001);await ports.anime.resolve({requestId:'p',seriesId:'s',seriesRevision:1,episodeIds:['x'],audio:'sub',quality:'best',subtitles:[],destination:{driveId:'files',path:''}});
  await ports.anime.getPlan('p');await ports.anime.enqueue({requestId:'j',planId:'p',planRevision:1});expect(await ports.anime.reconcile('j')).toBeNull();
  expect((await ports.anime.listJobs()).items).toHaveLength(1);await ports.anime.getJob('j');await ports.anime.control({id:'j',revision:1,action:'cancel'});
  expect(calls).toEqual(['getAnimeStatus','searchAnime','getAnimeSearch','lookupAnimeSeries','getAnimeSeries','getAnimeCover','resolveAnime','getAnimePlan','enqueueAnime','reconcileAnime','listAnimeJobs','getAnimeJob','controlAnimeJob']);
  ports.dispose();
 });
 it('rejects malformed, oversized or unsafe service responses',async()=>{
  const mock=createMockAnime();const job=await mock.enqueue({requestId:'j',planId:(await mock.resolve({requestId:'p',seriesId:(await mock.lookup({requestId:'s',anilistId:900001})).id,seriesRevision:1,episodeIds:['demo.1.1'],audio:'sub',quality:'best',subtitles:[],destination:{driveId:'files',path:''}})).id,planRevision:1});
  const bad:[string,unknown][]=[['getAnimeJob',{...job,_task:'secret'}],['getAnimeJob',{...job,episodes:[{...job.episodes[0],number:'1e3'}]}],['getAnimeJob',{...job,state:'exploded'}],
   ['getAnimeCover',{dataUrl:'data:image/svg+xml;base64,PHN2Zz4='}],['getAnimeCover',{dataUrl:'javascript:alert(1)'}],['getAnimeStatus',{enabled:true}]];
  for(const [action,value] of bad){
   const ports=createServiceBackend(bridgeWith(async()=>success(value)));
   const call=action==='getAnimeJob'?ports.anime.getJob('j'):action==='getAnimeCover'?ports.anime.cover(1):ports.anime.status();
   await expect(call,action+JSON.stringify(value).slice(0,80)).rejects.toThrow();ports.dispose();
  }
 });
 it('demo adapter is explicit that nothing is downloaded',async()=>{
  const mock=createMockAnime();expect((await mock.status()).message).toMatch(/no media is downloaded/);
  const series=await mock.lookup({requestId:'s',anilistId:900002});expect(series.match.state).toBe('ambiguous');
  const chosen=await mock.lookup({requestId:'s2',anilistId:900002,providerSeriesId:'demo/archive-lights-second-light-2'});expect(chosen.match.state).toBe('selected');
  const plan=await mock.resolve({requestId:'p',seriesId:chosen.id,seriesRevision:1,episodeIds:chosen.episodes.map(e=>e.id),audio:'dub',quality:'1080p',subtitles:[],destination:{driveId:'files',path:''}});
  expect(plan.counts).toMatchObject({conflict:1,unavailable:0});const job=await mock.enqueue({requestId:'j',planId:plan.id,planRevision:1});
  expect(job.state).toBe('paused');expect(job.episodes.filter(e=>e.state==='paused').every(e=>e.error==='Demo only — no files are created')).toBe(true);
 });
});
