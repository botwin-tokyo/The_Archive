// @vitest-environment node
import {createRequire} from 'node:module';
import {mkdtemp,writeFile,chmod,symlink,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {describe,expect,it,vi} from 'vitest';
const require=createRequire(import.meta.url);
const {readServiceConfig,createServiceClient,validateAction,MAX_CHUNK}=require('../../electron/service-client.cjs');
const TOKEN='fixture-not-a-production-credential';
describe('main-only service transport',()=>{
 it('defaults to no endpoint and rejects unsafe transport configuration',()=>{
  expect(readServiceConfig({})).toEqual({mode:'service',configured:false});expect(readServiceConfig({ARCHIVE_CONSOLE_MODE:'demo'})).toEqual({mode:'demo',configured:false});
  for(const endpoint of ['http://example.invalid','file:///etc/passwd','http://127.0.0.1/path','https://user:password@example.invalid','https://example.invalid/?token=secret'])expect(()=>readServiceConfig({ARCHIVE_SERVICE_ENDPOINT:endpoint,ARCHIVE_SERVICE_TOKEN:TOKEN})).toThrow();
  expect(readServiceConfig({ARCHIVE_SERVICE_ENDPOINT:'http://127.0.0.1:8787',ARCHIVE_SERVICE_TOKEN:TOKEN})).toMatchObject({configured:true,endpoint:'http://127.0.0.1:8787'});
 });
 it('requires a private owned regular credential file and never follows symlinks',async()=>{
  const root=await mkdtemp(path.join(tmpdir(),'archive-secret-test-'));try{const file=path.join(root,'credential');await writeFile(file,TOKEN,{mode:0o600});expect(readServiceConfig({ARCHIVE_SERVICE_ENDPOINT:'http://127.0.0.1:8787',ARCHIVE_SERVICE_TOKEN_FILE:file}).configured).toBe(true);await chmod(file,0o644);expect(()=>readServiceConfig({ARCHIVE_SERVICE_ENDPOINT:'http://127.0.0.1:8787',ARCHIVE_SERVICE_TOKEN_FILE:file})).toThrow();await chmod(file,0o600);const link=path.join(root,'link');await symlink(file,link);expect(()=>readServiceConfig({ARCHIVE_SERVICE_ENDPOINT:'http://127.0.0.1:8787',ARCHIVE_SERVICE_TOKEN_FILE:link})).toThrow();}finally{await rm(root,{recursive:true,force:true});}
 });
 it('exposes only known request shapes and rejects arbitrary paths, URLs, actions and fake handles',()=>{
  expect(validateAction('listDrives',{})).toEqual({});for(const [action,args]of [['invoke',{}],['listDrives',{url:'https://evil.invalid'}],['selectNative',{kind:'files',path:'/etc'}],['statFile',{ref:{id:'x',driveId:'d',path:'../secret',revision:1}}],['submitOperation',{request:{requestId:'r',kind:'upload',selection:{handle:'native:fake',kind:'files',simulated:false,items:[],label:'fake'}}}]])expect(()=>validateAction(action,args)).toThrow();
 });
 it('sends credentials only from main, blocks redirects and limits responses and chunks',async()=>{
  const fetcher=vi.fn(async(_url:string,_options:unknown)=>new Response(JSON.stringify({ok:true,result:[]}),{status:200}));const client=createServiceClient({configured:true,endpoint:'http://127.0.0.1:8787',token:TOKEN},fetcher);await client.call('drives.list',{});expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8787/v1/call');expect(fetcher.mock.calls[0][1]).toMatchObject({redirect:'error',headers:{Authorization:`Bearer ${TOKEN}`}});
  expect(()=>client.upload('transfer','0',Buffer.alloc(MAX_CHUNK+1))).toThrow();
  const oversized=createServiceClient({configured:true,endpoint:'http://127.0.0.1',token:TOKEN},async()=>new Response('x',{headers:{'content-length':String(9*1024*1024)}}));await expect(oversized.call('drives.list',{})).rejects.toMatchObject({detail:{code:'INVALID_RESPONSE'}});
 });
});

describe('exact-target native purge bridge',()=>{
 const {submitPurge}=require('../../electron/service-client.cjs');
 const target={id:'trash-1',driveId:'files',path:'old.bin',revision:3};
 const request={requestId:'purge-request',kind:'purge',targets:[target],confirmation:{kind:'permanent-delete',driveId:'files',targets:[target],acknowledged:true}};
 it('issues a grant only after exact acknowledgement and keeps it within main',async()=>{const client={call:vi.fn(async(capability:string,args:Record<string,unknown>)=>{if(capability==='operations.reconcile')return null;if(capability==='trash.confirm')return {grantId:'one-time-fixture-grant'};expect(args).toMatchObject({request:{confirmation:{grantId:'one-time-fixture-grant'}}});return {status:'accepted',requestId:'purge-request',jobIds:['purge-job'],simulated:false};})};const receipt=await submitPurge(client,request);expect(receipt.status).toBe('accepted');expect(JSON.stringify(receipt)).not.toContain('grant');expect(client.call.mock.calls.map(([name])=>name)).toEqual(['operations.reconcile','trash.confirm','operations.submit']);});
 it('rejects a stale/different acknowledged target without any service mutation',async()=>{const client={call:vi.fn()};await expect(submitPurge(client,{...request,confirmation:{...request.confirmation,targets:[{...target,revision:4}]}})).rejects.toMatchObject({detail:{code:'CONFIRMATION_REQUIRED'}});expect(client.call).not.toHaveBeenCalled();});
 it('reconciles an accepted idempotent purge without requesting another grant',async()=>{const receipt={status:'accepted',jobIds:['old-job']};const client={call:vi.fn(async()=>receipt)};expect(await submitPurge(client,request)).toBe(receipt);expect(client.call).toHaveBeenCalledTimes(1);});
});
