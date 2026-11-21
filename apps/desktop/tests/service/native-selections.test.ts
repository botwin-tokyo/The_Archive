// @vitest-environment node
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {mkdtemp,writeFile,readFile,readdir,mkdir,symlink,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {afterEach,describe,expect,it,vi} from 'vitest';
const require=createRequire(import.meta.url);
const {createNativeSelections}=require('../../electron/native-selections.cjs');
const roots:string[]=[];
async function fixture(){const root=await mkdtemp(path.join(tmpdir(),'archive-native-test-'));roots.push(root);return root;}
afterEach(async()=>{for(const root of roots.splice(0))await rm(root,{recursive:true,force:true});});
async function idle(native:{debugCounts():{running:number}}){await vi.waitFor(()=>expect(native.debugCounts().running).toBe(0),{timeout:10000,interval:10});}
function picker(paths:string[]){return {showOpenDialog:async()=>({canceled:false,filePaths:paths})};}
function uploadClient(){const chunks:Buffer[]=[];let offset=0;const errors:unknown[]=[];const call=vi.fn(async(capability:string,args:Record<string,unknown>):Promise<Record<string,unknown>>=>{if(capability==='transfers.initiate')return {transferId:'transfer-1',jobId:'job-1',offset:'0',chunkBytes:1024*1024};if(capability==='transfers.query')return {transferId:'transfer-1',offset:String(offset),state:'waiting-for-client'};if(capability==='transfers.finalize')return {jobId:'job-1',verification:{algorithm:'sha256',digest:createHash('sha256').update(Buffer.concat(chunks)).digest('hex')}};if(capability==='transfers.clientError'){errors.push(args);return {};}throw new Error(capability);});const upload=vi.fn(async(_id:string,position:string,chunk:Buffer)=>{expect(position).toBe(String(offset));expect(chunk.length).toBeLessThanOrEqual(1024*1024);chunks.push(Buffer.from(chunk));offset+=chunk.length;return {transferId:'transfer-1',offset:String(offset)};});return {call,upload,chunks,errors};}
describe('bounded native selection and streaming',()=>{
 it('recovers a lost chunk acknowledgement and a slow finalization without duplicate bytes or manual resume',async()=>{
  const root=await fixture(),source=path.join(root,'reconnect.bin');const bytes=Buffer.alloc(1024*1024+31,47);await writeFile(source,bytes);
  const client=uploadClient(),originalCall=client.call,originalUpload=client.upload;let lostChunk=false,lostFinalize=false,settling=0;
  client.call=vi.fn(async(capability,args)=>{
   if(capability==='jobs.get')return {state:settling-->0?'running':'waiting-for-client'};
   if(capability==='transfers.finalize'&&!lostFinalize){lostFinalize=true;settling=1;throw {detail:{code:'TIMEOUT',retryable:true}};}
   const result=await originalCall(capability,args);return capability==='transfers.query'?{...result,jobId:'job-1'}:result;
  });
  client.upload=vi.fn(async(id,offset,chunk)=>{
   if(lostChunk&&offset==='0')return {transferId:id,offset:String(chunk.length)};
   const result=await originalUpload(id,offset,chunk);
   if(!lostChunk){lostChunk=true;settling=1;throw {detail:{code:'UNAVAILABLE',retryable:true}};}
   return result;
  });
  const delay=vi.fn(async()=>{});const native=createNativeSelections({dialog:picker([source]),getWindow:()=>null,client,retryDelay:delay});
  try{const selection=await native.select('files');await native.submit({requestId:'reconnect',kind:'upload',destination:{driveId:'files',path:''},selection});await idle(native);expect(Buffer.concat(client.chunks)).toEqual(bytes);expect(client.errors).toEqual([]);expect(delay).toHaveBeenCalledTimes(4);expect(originalCall.mock.calls.filter(([c])=>c==='transfers.initiate')).toHaveLength(1);expect(originalCall.mock.calls.some(([c])=>c==='transfers.finalize')).toBe(true);}finally{await native.dispose();}
 });
 it('stops reconnecting when disposed and does not retry integrity failures',async()=>{
  const root=await fixture(),source=path.join(root,'stop.bin');await writeFile(source,'data');const client=uploadClient();let release=()=>{};
  const delay=vi.fn(()=>new Promise<void>(resolve=>{release=resolve;}));client.upload=vi.fn(async()=>{throw {detail:{code:'UNAVAILABLE',retryable:true}};});
  const native=createNativeSelections({dialog:picker([source]),getWindow:()=>null,client,retryDelay:delay});
  const selection=await native.select('files');await native.submit({requestId:'stop',kind:'upload',destination:{driveId:'files',path:''},selection});await vi.waitFor(()=>expect(delay).toHaveBeenCalledOnce());await native.dispose();release();await idle(native);expect(client.upload).toHaveBeenCalledOnce();
  const failed=uploadClient();failed.upload=vi.fn(async()=>{throw {detail:{code:'INTEGRITY_FAILED',retryable:true}};});const noRetry=vi.fn(async()=>{});const second=createNativeSelections({dialog:picker([source]),getWindow:()=>null,client:failed,retryDelay:noRetry});
  try{const selected=await second.select('files');await second.submit({requestId:'integrity',kind:'upload',destination:{driveId:'files',path:''},selection:selected});await idle(second);expect(noRetry).not.toHaveBeenCalled();expect(failed.errors).toContainEqual({transferId:'transfer-1',code:'INTEGRITY_FAILED'});}finally{await second.dispose();}
 });
 it('returns opaque metadata, streams multiple bounded ranges, and verifies bytes',async()=>{
  const root=await fixture(),source=path.join(root,'opaque.bin');const bytes=Buffer.alloc(3*1024*1024+17,173);await writeFile(source,bytes);const client=uploadClient();const native=createNativeSelections({dialog:picker([source]),getWindow:()=>null,client});try{const selection=await native.select('files');expect(selection.handle).toMatch(/^native:/);expect(selection.simulated).toBe(false);expect(JSON.stringify(selection)).not.toContain(root);expect(selection.items).toEqual([{relativePath:'opaque.bin',sizeBytes:String(bytes.length)}]);const receipt=await native.submit({requestId:'r',kind:'upload',destination:{driveId:'files',path:''},selection});expect(receipt).toMatchObject({status:'accepted',jobIds:['job-1'],simulated:false});await idle(native);expect(client.call.mock.calls.find(([capability])=>capability==='transfers.initiate')?.[1]).toMatchObject({sha256:createHash('sha256').update(bytes).digest('hex')});expect(client.upload).toHaveBeenCalledTimes(4);expect(Buffer.concat(client.chunks).equals(bytes)).toBe(true);expect(client.errors).toEqual([]);expect(native.reconcile('r')).toEqual(receipt);}finally{await native.dispose();}
 });
 it('rejects symlink selections and expired or forged handles',async()=>{
  const root=await fixture(),source=path.join(root,'file.bin'),link=path.join(root,'link.bin');await writeFile(source,'opaque');await symlink(source,link);let now=0;const client=uploadClient();const unsafe=createNativeSelections({dialog:picker([link]),getWindow:()=>null,client});await expect(unsafe.select('files')).rejects.toMatchObject({detail:{code:'UNSUPPORTED'}});await unsafe.dispose();const native=createNativeSelections({dialog:picker([source]),getWindow:()=>null,client,now:()=>now});try{const selection=await native.select('files');now=31*60*1000;await expect(native.submit({requestId:'r',kind:'upload',selection,destination:{driveId:'files',path:''}})).rejects.toMatchObject({detail:{code:'INVALID_REQUEST'}});expect(client.upload).not.toHaveBeenCalled();}finally{await native.dispose();}
 });
 it('fails changed-source verification before finalization',async()=>{
  const root=await fixture(),source=path.join(root,'source.bin');await writeFile(source,Buffer.alloc(2*1024*1024,1));const client=uploadClient();const upload=client.upload;client.upload=vi.fn(async(id,offset,chunk)=>{const result=await upload(id,offset,chunk);if(offset==='0')await writeFile(source,Buffer.alloc(2*1024*1024,2));return result;});const native=createNativeSelections({dialog:picker([source]),getWindow:()=>null,client});try{const selection=await native.select('files');await native.submit({requestId:'r',kind:'upload',destination:{driveId:'files',path:''},selection});await idle(native);expect(client.call.mock.calls.some(([capability])=>capability==='transfers.finalize')).toBe(false);expect(client.errors).toContainEqual({transferId:'transfer-1',code:'SOURCE_CHANGED'});}finally{await native.dispose();}
 });
 it('streams downloads into an exclusive stage and publishes only verified complete bytes',async()=>{
  const root=await fixture();const bytes=Buffer.alloc(2*1024*1024+23,76);const ref={id:'e',driveId:'files',path:'opaque.bin',revision:7};let offset='0';const progress:unknown[]=[];const client={call:vi.fn(async(capability:string,args:Record<string,unknown>)=>{if(capability==='files.stat')return {...ref,name:'opaque.bin',kind:'file',sizeBytes:String(bytes.length)};if(capability==='transfers.download')return {jobId:'j',transferId:'t',sizeBytes:String(bytes.length),revision:7};if(capability==='transfers.query')return {offset,state:'waiting-for-client'};if(capability==='transfers.downloadProgress'){offset=String(args.offset);progress.push(args);return {};}throw new Error(capability);}),download:vi.fn(async(_ref:unknown,position:string,length:number)=>({bytes:bytes.subarray(Number(position),Number(position)+length),revision:'7'}))};const native=createNativeSelections({dialog:picker([root]),getWindow:()=>null,client});try{const selection=await native.select('download-destination');await native.submit({requestId:'d',kind:'download',targets:[ref],selection});await idle(native);expect((await readFile(path.join(root,'opaque.bin'))).equals(bytes)).toBe(true);expect(client.download).toHaveBeenCalledTimes(3);expect(progress.at(-1)).toEqual({transferId:'t',offset:String(bytes.length),complete:true});expect(await readdir(root)).toEqual(['opaque.bin']);}finally{await native.dispose();}
 });
 it('never overwrites an existing native destination',async()=>{
  const root=await fixture();await writeFile(path.join(root,'opaque.bin'),'keep me');const ref={id:'e',driveId:'files',path:'opaque.bin',revision:1};const errors:unknown[]=[];let offset='0';const client={call:async(capability:string,args:Record<string,unknown>)=>{if(capability==='files.stat')return {...ref,name:'opaque.bin',kind:'file',sizeBytes:'3'};if(capability==='transfers.download')return {jobId:'j',transferId:'t',sizeBytes:'3',revision:1};if(capability==='transfers.query')return {offset,state:'waiting-for-client'};if(capability==='transfers.downloadProgress'){offset=String(args.offset);return {};}if(capability==='transfers.clientError'){errors.push(args);return {};}throw new Error(capability);},download:async()=>({bytes:Buffer.from('new'),revision:'1'})};const native=createNativeSelections({dialog:picker([root]),getWindow:()=>null,client});try{const selection=await native.select('download-destination');await native.submit({requestId:'d',kind:'download',targets:[ref],selection,conflictPolicy:'replace'});await idle(native);expect(await readFile(path.join(root,'opaque.bin'),'utf8')).toBe('keep me');expect(errors).toContainEqual({transferId:'t',code:'CONFLICT'});}finally{await native.dispose();}expect(await readdir(root)).toEqual(['opaque.bin']);
 });
 it('rejects symlinked descendants during folder selection',async()=>{
  const root=await fixture(),folder=path.join(root,'selected');await mkdir(folder);await symlink(root,path.join(folder,'escape'));const native=createNativeSelections({dialog:picker([folder]),getWindow:()=>null,client:uploadClient()});try{await expect(native.select('folder')).rejects.toMatchObject({detail:{code:'UNSUPPORTED'}});}finally{await native.dispose();}
 });
 it('resumes an explicitly paused upload from the durable offset without reselecting or repeating acknowledged bytes',async()=>{
  const root=await fixture(),source=path.join(root,'resume.bin');const bytes=Buffer.alloc(3*1024*1024+11);for(let i=0;i<bytes.length;i++)bytes[i]=i%251;await writeFile(source,bytes);const client=uploadClient();const originalCall=client.call,originalUpload=client.upload;let state='waiting-for-client',pausedOnce=false;
  client.call=vi.fn(async(capability,args)=>{const result=await originalCall(capability,args);return capability==='transfers.query'?{...result,state}:result;});
  client.upload=vi.fn(async(id,offset,chunk)=>{if(state==='paused')throw {detail:{code:'ABORTED'}};const result=await originalUpload(id,offset,chunk);if(!pausedOnce){pausedOnce=true;state='paused';}return result;});
  const native=createNativeSelections({dialog:picker([source]),getWindow:()=>null,client});try{const selection=await native.select('files');await native.submit({requestId:'pause-r',kind:'upload',selection,destination:{driveId:'files',path:''}});await idle(native);expect(client.chunks).toHaveLength(1);expect(client.errors).toEqual([]);state='waiting-for-client';native.resumeJob('job-1','resume');await idle(native);expect(Buffer.concat(client.chunks).equals(bytes)).toBe(true);expect(client.errors).toEqual([]);expect(originalCall.mock.calls.some(([capability])=>capability==='transfers.finalize')).toBe(true);}finally{await native.dispose();}
 });

 it('selects more than 1000 folder entries without retaining one descriptor per file',async()=>{
  const root=await fixture(),folder=path.join(root,'external');await mkdir(folder);
  for(let n=0;n<1100;n++)await writeFile(path.join(folder,`${n}.json`),'card');
  const native=createNativeSelections({dialog:picker([folder]),getWindow:()=>null,client:uploadClient()});
  try{const selection=await native.select('folder');expect(selection.totalFiles).toBe(1100);expect(selection.totalBytes).toBe('4400');expect(selection.items).toHaveLength(100);expect(native.debugCounts().opened).toBe(1);}finally{await native.dispose();}
 });

});
