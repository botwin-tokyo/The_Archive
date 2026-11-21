// @vitest-environment node
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';
import {afterAll,beforeAll,describe,expect,it,vi} from 'vitest';
import {createServiceBackend,SERVICE_ACTIONS,type DesktopBridge} from '../../src/adapters/service/createServiceBackend';
const require=createRequire(import.meta.url);
const {createServiceClient,ACTIONS,safeFailure,validateAction}=require('../../electron/service-client.cjs');
const TOKEN='desktop-test-fixture-owner-credential-00000000',AGENT='desktop-test-fixture-agent-credential-00000000';
const ANIME_ACTIONS=Object.keys(ACTIONS).filter(name=>String(ACTIONS[name][0]).startsWith('anime.'));
let process_:ChildProcessWithoutNullStreams,port=0;
let ports:ReturnType<typeof createServiceBackend>;
let client:ReturnType<typeof createServiceClient>;
beforeAll(async()=>{
 process_=spawn('python',['apps/desktop/tests/service/anime_fixture_service.py'],{env:{...process.env,PYTHONPATH:path.resolve('apps/backend/src')},stdio:'pipe'});
 ({port}=await new Promise<{port:number}>((resolve,reject)=>{let stdout='',stderr='';const timeout=setTimeout(()=>reject(new Error('Fixture startup timed out: '+stderr)),15000);process_.stderr.on('data',chunk=>{stderr+=chunk;});process_.stdout.on('data',chunk=>{stdout+=chunk;if(stdout.includes('\n')){clearTimeout(timeout);try{resolve(JSON.parse(stdout.split('\n')[0]));}catch(cause){reject(cause);}}});process_.once('exit',()=>{clearTimeout(timeout);reject(new Error('Fixture exited: '+stderr));});}));
 client=createServiceClient({configured:true,endpoint:`http://127.0.0.1:${port}`,token:TOKEN});
 const bridge:DesktopBridge={mode:'service',getVersion:async()=>({schemaVersion:1,appVersion:'fixture',mode:'service',configured:true}),backend:Object.fromEntries(SERVICE_ACTIONS.map(action=>[action,async(args:unknown)=>{try{return {version:1,mode:'service',ok:true,result:await client.call(ACTIONS[action][0],validateAction(action,args))};}catch(cause){return {version:1,mode:'service',ok:false,error:safeFailure(cause)};}}])) as DesktopBridge['backend']};
 ports=createServiceBackend(bridge);
},20000);
afterAll(async()=>{ports?.dispose();if(process_&&!process_.killed){process_.stdin.end('\n');await new Promise<void>(resolve=>{process_.once('exit',()=>resolve());setTimeout(()=>{process_.kill();resolve();},2000);});}});
async function mcp(body:unknown,token=TOKEN){
 const response=await fetch(`http://127.0.0.1:${port}/mcp`,{method:'POST',headers:{Authorization:`Bearer ${token}`,'Content-Type':'application/json',Accept:'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'},body:JSON.stringify(body)});
 return response.json();
}
describe('anime ports against the independent Python HTTP service',()=>{
 it('registers every anime capability on the bridge, preload list and renderer actions',()=>{
  expect(ANIME_ACTIONS.sort()).toEqual(['controlAnimeJob','enqueueAnime','getAnimeCover','getAnimeJob','getAnimePlan','getAnimeSearch','getAnimeSeries','getAnimeStatus','listAnimeJobs','lookupAnimeSeries','reconcileAnime','resolveAnime','searchAnime']);
  for(const action of ANIME_ACTIONS)expect(SERVICE_ACTIONS).toContain(action);
 });
 it('searches, looks up, reviews, enqueues and publishes a verified episode through the real service',async()=>{
  const status=await ports.anime.status();expect(status).toMatchObject({enabled:true,provider:'ani.pm',catalog:'AniList',mcp:true});
  await vi.waitFor(async()=>expect((await ports.anime.status()).available).toBe(true),{timeout:5000,interval:100});
  const search=await ports.anime.search({requestId:'ui-search',query:'Frieren'});
  await vi.waitFor(async()=>expect((await ports.anime.getSearch(search.id)).state).toBe('completed'),{timeout:5000,interval:100});
  expect((await ports.anime.getSearch(search.id)).items[0]).toMatchObject({anilistId:154587,title:'Fixture Frieren'});
  expect((await ports.anime.cover(154587)).dataUrl).toMatch(/^data:image\/png;base64,/);
  const pending=await ports.anime.lookup({requestId:'ui-lookup',anilistId:154587});
  await vi.waitFor(async()=>expect((await ports.anime.getSeries(pending.id)).state).toBe('completed'),{timeout:5000,interval:100});
  const series=await ports.anime.getSeries(pending.id);expect(series.match).toEqual({state:'matched',providerSeriesId:'settlar/fixture-frieren-6351',evidence:'anilist-id'});
  const plan=await ports.anime.resolve({requestId:'ui-plan',seriesId:series.id,seriesRevision:series.revision,episodeIds:['e1.one','e1.two'],audio:'dub',quality:'1080p',subtitles:['en'],destination:{driveId:'files',path:''}});
  expect(plan.counts).toEqual({total:2,new:1,'already-present':0,conflict:0,unavailable:1});expect(plan.episodes.map(e=>e.name)).toEqual(['Episode 001 [dub].mp4','Episode 002 [dub].mp4']);
  const job=await ports.anime.enqueue({requestId:'ui-enqueue',planId:plan.id,planRevision:plan.revision});
  expect(await ports.anime.enqueue({requestId:'ui-enqueue',planId:plan.id,planRevision:plan.revision})).toMatchObject({id:job.id});
  await vi.waitFor(async()=>expect((await ports.anime.getJob(job.id)).state).toBe('partial'),{timeout:10000,interval:100});
  const done=await ports.anime.getJob(job.id);expect(done.episodes.map(e=>e.state)).toEqual(['completed','unavailable']);expect(done.summary).toMatchObject({completed:1,unavailable:1,pending:0});
  const folder=(await ports.files.list({location:{driveId:'files',path:done.folder},requestId:'anime-folder'})).entries.map(e=>e.name);expect(folder).toEqual(['Episode 001 [dub].mp4']);
  expect(await ports.anime.reconcile('ui-enqueue')).toEqual({action:'enqueue',id:job.id,state:'partial'});expect(await ports.anime.reconcile('never-sent')).toBeNull();
  expect((await ports.anime.listJobs()).items[0].id).toBe(job.id);
  await expect(ports.anime.control({id:job.id,revision:done.revision+5,action:'retry-failed'})).rejects.toMatchObject({detail:{code:'STALE_REVISION'}});
 });
 it('rejects host paths, URLs, oversized and unknown fields before the service',()=>{
  const base={requestId:'r',seriesId:'s',seriesRevision:1,episodeIds:['e1.one'],audio:'sub',quality:'1080p',subtitles:['en'],destination:{driveId:'files',path:''}};
  for(const args of [{...base,destination:{driveId:'files',path:'/etc'}},{...base,destination:{driveId:'files',path:'../x'}},{...base,episodeIds:['https://evil.example/e']},{...base,quality:'4k'},{...base,subtitles:['xx']},{...base,audio:'raw'},{...base,episodeIds:Array.from({length:201},(_,i)=>'e'+i)},{...base,url:'https://x'}])
   expect(()=>validateAction('resolveAnime',args)).toThrow();
  expect(()=>validateAction('lookupAnimeSeries',{requestId:'r',anilistId:1,providerSeriesId:'https://evil.example/x-1'})).toThrow();
  expect(()=>validateAction('searchAnime',{requestId:'r',query:'x'.repeat(201)})).toThrow();
 });
 it('denies non-owner identities and validates malformed nested input in the service',async()=>{
  const agent=createServiceClient({configured:true,endpoint:`http://127.0.0.1:${port}`,token:AGENT});
  await expect(agent.call('anime.status',{})).rejects.toMatchObject({detail:{code:'PERMISSION_DENIED'}});
  await expect(client.call('anime.resolve',{requestId:'bad',seriesId:'missing',seriesRevision:1,episodeIds:['e1'],audio:'sub',quality:'best',subtitles:[],destination:{driveId:'files',path:''},extra:1})).rejects.toMatchObject({detail:{code:'INVALID_REQUEST'}});
  await expect(client.call('anime.series.lookup',{requestId:'bad-id',anilistId:'154587'})).rejects.toMatchObject({detail:{code:'INVALID_REQUEST'}});
 });
 it('exposes the same anime tools and results through MCP',async()=>{
  const tools=(await mcp({jsonrpc:'2.0',id:1,method:'tools/list'})).result.tools as {name:string;inputSchema:{properties:Record<string,unknown>;required:string[]};annotations:{readOnlyHint:boolean}}[];
  const anime=tools.filter(t=>t.name.startsWith('anime.'));expect(anime.map(t=>t.name).sort()).toEqual(ANIME_ACTIONS.map(a=>ACTIONS[a][0]).sort());
  const resolve=anime.find(t=>t.name==='anime.resolve')!;expect(resolve.inputSchema.required).toContain('episodeIds');expect(resolve.inputSchema.properties.destination).toMatchObject({required:['driveId','path']});
  const desktop=await ports.anime.listJobs();const call=await mcp({jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'anime.jobs.list',arguments:{}}});
  expect(call.result.isError).toBe(false);expect(call.result.structuredContent.items.map((j:{id:string})=>j.id)).toEqual(desktop.items.map(j=>j.id));
  const denied=await mcp({jsonrpc:'2.0',id:3,method:'tools/call',params:{name:'anime.status',arguments:{}}},AGENT);expect(denied.result.isError).toBe(true);
 });
});
