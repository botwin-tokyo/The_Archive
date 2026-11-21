// @vitest-environment node
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {mkdtemp,writeFile,readFile,mkdir,readdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {afterEach,describe,expect,it,vi} from 'vitest';
const require=createRequire(import.meta.url);
const {createNativeSelections}=require('../../electron/native-selections.cjs');
const roots:string[]=[];
const digest=(bytes:Buffer)=>createHash('sha256').update(bytes).digest('hex');
async function fixture(){const root=await mkdtemp(path.join(tmpdir(),'archive-native-reselection-'));roots.push(root);return root;}
afterEach(async()=>{for(const root of roots.splice(0))await rm(root,{recursive:true,force:true});});
const choose=(paths:string[])=>({showOpenDialog:vi.fn(async()=>({canceled:false,filePaths:paths})),showMessageBox:vi.fn(async()=>({response:0}))});
async function idle(manager:{debugCounts():{running:number}}){await vi.waitFor(()=>expect(manager.debugCounts().running).toBe(0),{timeout:10000,interval:10});}
function existingUpload(bytes:Buffer,initialOffset=3,sha256:string|null=digest(bytes),action:'resume'|'retry'='resume'){
 const pieces=[bytes.subarray(0,initialOffset)];let offset=initialOffset;
 const call=vi.fn(async(capability:string,args:Record<string,unknown>):Promise<Record<string,unknown>>=>{
  if(capability==='transfers.resumeInfo')return {transferId:'existing-transfer',jobId:'existing-job',direction:'upload',name:'original.bin',sizeBytes:String(bytes.length),offset:String(offset),sha256,ref:null,state:'waiting-for-client'};
  if(capability==='jobs.control'){expect(args).toEqual({id:'existing-job',action});return {id:'existing-job',state:'waiting-for-client'};}
  if(capability==='transfers.query')return {offset:String(offset),state:'waiting-for-client'};
  if(capability==='transfers.finalize')return {jobId:'existing-job',verification:{algorithm:'sha256',digest:digest(Buffer.concat(pieces))}};
  if(capability==='transfers.clientError')return {};
  throw Error('Unexpected capability '+capability);
 });
 const upload=vi.fn(async(id:string,position:string,chunk:Buffer)=>{expect(id).toBe('existing-transfer');expect(position).toBe(String(offset));expect(chunk.length).toBeLessThanOrEqual(1024*1024);pieces.push(Buffer.from(chunk));offset+=chunk.length;return {transferId:id,offset:String(offset)};});
 return {call,upload,pieces,getOffset:()=>offset};
}
describe('reselect existing native transfers after desktop recreation',()=>{
 it('verifies full size and SHA256, then resumes the same upload at an arbitrary acknowledged byte offset',async()=>{
  const root=await fixture(),source=path.join(root,'renamed-but-identical.bin');const bytes=Buffer.alloc(2*1024*1024+29);for(let i=0;i<bytes.length;i++)bytes[i]=i%251;await writeFile(source,bytes);const client=existingUpload(bytes);const dialog=choose([source]);const manager=createNativeSelections({dialog,getWindow:()=>null,client});try{const result=await manager.controlJob('existing-job','resume');expect(result.id).toBe('existing-job');await idle(manager);expect(Buffer.concat(client.pieces).equals(bytes)).toBe(true);expect(client.upload.mock.calls[0][1]).toBe('3');expect(client.upload.mock.calls[0][2].length).toBe(1024*1024-3);expect(client.call.mock.calls.map(([capability])=>capability)).not.toContain('transfers.initiate');expect(dialog.showOpenDialog).toHaveBeenCalledWith(null,expect.objectContaining({title:'Reselect the original file to resume this upload',buttonLabel:'Verify and resume',properties:['openFile']}));}finally{await manager.dispose();}
 });
 it('rejects same-size different content before job control or transfer bytes',async()=>{
  const root=await fixture(),source=path.join(root,'wrong.bin');const original=Buffer.from('original bytes');await writeFile(source,Buffer.alloc(original.length,8));const client=existingUpload(original),dialog=choose([source]);const manager=createNativeSelections({dialog,getWindow:()=>null,client});try{await expect(manager.controlJob('existing-job','resume')).rejects.toMatchObject({detail:{code:'SOURCE_CHANGED'}});expect(client.call.mock.calls.map(([capability])=>capability)).toEqual(['transfers.resumeInfo']);expect(client.upload).not.toHaveBeenCalled();expect(client.getOffset()).toBe(3);expect(manager.debugCounts().handles).toBe(0);expect(dialog.showMessageBox).toHaveBeenCalledWith(null,expect.objectContaining({title:'Upload source does not match',message:expect.stringContaining('job and acknowledged bytes were not changed')}));}finally{await manager.dispose();}
 });
 it('cancels reselection without changing the existing job or offset',async()=>{
  const client=existingUpload(Buffer.from('original')),dialog={showOpenDialog:vi.fn(async()=>({canceled:true,filePaths:[]}))};const manager=createNativeSelections({dialog,getWindow:()=>null,client});try{await expect(manager.controlJob('existing-job','resume')).rejects.toMatchObject({detail:{code:'ABORTED'}});expect(client.call.mock.calls.map(([capability])=>capability)).toEqual(['transfers.resumeInfo']);expect(client.upload).not.toHaveBeenCalled();expect(client.getOffset()).toBe(3);}finally{await manager.dispose();}
 });
 it('refuses a legacy upload without a recorded source hash before opening a picker',async()=>{
  const client=existingUpload(Buffer.from('legacy upload'),3,null),dialog=choose([]);const manager=createNativeSelections({dialog,getWindow:()=>null,client});try{await expect(manager.controlJob('existing-job','resume')).rejects.toMatchObject({detail:{code:'UNSUPPORTED'}});expect(dialog.showOpenDialog).not.toHaveBeenCalled();expect(client.upload).not.toHaveBeenCalled();expect(client.call).toHaveBeenCalledTimes(1);}finally{await manager.dispose();}
 });
 it('does not offer a source picker for ordinary service-local job resume',async()=>{
  const call=vi.fn(async(capability:string)=>{if(capability==='transfers.resumeInfo')throw {detail:{code:'UNSUPPORTED'}};return {id:'copy-job',state:'queued'};});const dialog=choose([]),manager=createNativeSelections({dialog,getWindow:()=>null,client:{call}});try{expect(await manager.controlJob('copy-job','resume')).toMatchObject({id:'copy-job'});expect(dialog.showOpenDialog).not.toHaveBeenCalled();expect(call.mock.calls.map(([capability])=>capability)).toEqual(['transfers.resumeInfo','jobs.control']);}finally{await manager.dispose();}
 });
 it.each(['resume','retry'] as const)('explicitly restarts a lost-local-partial download at byte0 into a newly selected folder on %s',async(action)=>{
  const root=await fixture(),destination=path.join(root,'new-destination');await mkdir(destination);const bytes=Buffer.from('stable source bytes'),ref={id:'source',driveId:'files',path:'source.bin',revision:4};let offset='6';const call=vi.fn(async(capability:string,args:Record<string,unknown>):Promise<Record<string,unknown>>=>{if(capability==='transfers.resumeInfo')return {transferId:'download-session',jobId:'download-job',direction:'download',name:'source.bin',sizeBytes:String(bytes.length),offset,sha256:null,ref,state:'waiting-for-client'};if(capability==='files.stat')return {...ref,kind:'file',sizeBytes:String(bytes.length)};if(capability==='transfers.restartDownload'){offset='0';return {offset};}if(capability==='jobs.control')return {id:'download-job',state:'waiting-for-client'};if(capability==='transfers.query')return {offset,state:'waiting-for-client'};if(capability==='transfers.downloadProgress'){offset=String(args.offset);return {offset};}if(capability==='transfers.clientError')return {};throw Error(capability);});const download=vi.fn(async(_ref:unknown,position:string,length:number)=>({bytes:bytes.subarray(Number(position),Number(position)+length),revision:'4'}));const dialog=choose([destination]),manager=createNativeSelections({dialog,getWindow:()=>null,client:{call,download}});try{await manager.controlJob('download-job',action);await idle(manager);expect((await readFile(path.join(destination,'source.bin'))).equals(bytes)).toBe(true);expect(download.mock.calls[0][1]).toBe('0');expect(call.mock.calls.find(([capability])=>capability==='jobs.control')).toEqual(['jobs.control',{id:'download-job',action:'resume'}]);expect(call.mock.calls.map(([capability])=>capability).slice(0,4)).toEqual(['transfers.resumeInfo','files.stat','transfers.restartDownload','jobs.control']);expect(call.mock.calls.map(([capability])=>capability)).not.toContain('transfers.download');expect(dialog.showOpenDialog).toHaveBeenCalledWith(null,expect.objectContaining({title:'Restart this existing download from byte 0',buttonLabel:'Restart from byte 0'}));expect(await readdir(destination)).toEqual(['source.bin']);}finally{await manager.dispose();}
 });
 it('never resets a download if destination reselection is canceled',async()=>{
  const ref={id:'source',driveId:'files',path:'source.bin',revision:1};const call=vi.fn(async(capability:string)=>capability==='transfers.resumeInfo'?{transferId:'d',jobId:'j',direction:'download',name:'source.bin',sizeBytes:'8',offset:'4',sha256:null,ref,state:'waiting-for-client'}:{...ref,kind:'file',sizeBytes:'8'});const manager=createNativeSelections({dialog:{showOpenDialog:async()=>({canceled:true,filePaths:[]})},getWindow:()=>null,client:{call}});try{await expect(manager.controlJob('j','resume')).rejects.toMatchObject({detail:{code:'ABORTED'}});expect(call.mock.calls.map(([capability])=>capability)).toEqual(['transfers.resumeInfo','files.stat']);}finally{await manager.dispose();}
 });
 it('reselects and verifies a failed upload before Retry when no native source map remains',async()=>{
  const root=await fixture(),source=path.join(root,'retry-source.bin'),bytes=Buffer.from('verified retry source');await writeFile(source,bytes);const client=existingUpload(bytes,3,digest(bytes),'retry');const dialog=choose([source]);const manager=createNativeSelections({dialog,getWindow:()=>null,client});try{const result=await manager.controlJob('existing-job','retry');expect(result.id).toBe('existing-job');await idle(manager);expect(Buffer.concat(client.pieces).equals(bytes)).toBe(true);expect(client.call.mock.calls[0]).toEqual(['transfers.resumeInfo',{jobId:'existing-job'}]);expect(client.call.mock.calls.find(([capability])=>capability==='jobs.control')).toEqual(['jobs.control',{id:'existing-job',action:'retry'}]);expect(client.call.mock.calls.some(([capability])=>capability==='transfers.initiate')).toBe(false);expect(dialog.showOpenDialog).toHaveBeenCalledWith(null,expect.objectContaining({buttonLabel:'Verify and retry'}));}finally{await manager.dispose();}
 });

});
