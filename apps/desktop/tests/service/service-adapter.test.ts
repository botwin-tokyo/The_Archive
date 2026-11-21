import {afterEach,describe,expect,it,vi} from 'vitest';
import {createServiceBackend,SERVICE_ACTIONS,type DesktopBridge} from '../../src/adapters/service/createServiceBackend';
import {createMockBackend} from '../../src/adapters/mock';
import {createDependencies} from '../../src/app/createDependencies';
const success=(result:unknown)=>({version:1,mode:'service',ok:true,result});
function real<T>(value:T):T{return JSON.parse(JSON.stringify(value).replaceAll('"simulated":true','"simulated":false').replaceAll('DEMO · nonfunctional placeholder','Stored securely outside renderer'));}
function bridgeWith(handler:(action:string,args:unknown)=>Promise<unknown>):DesktopBridge{return {mode:'service',getVersion:async()=>({schemaVersion:1,appVersion:'test',mode:'service',configured:true}),backend:Object.fromEntries(SERVICE_ACTIONS.map(action=>[action,(args:unknown)=>handler(action,args)])) as DesktopBridge['backend']};}
afterEach(()=>{vi.useRealTimers();delete window.archiveDesktop;});
describe('production service ports',()=>{
 it('maps all ports to named bridge calls and validates real contracts',async()=>{
  const mock=createMockBackend();const calls:{action:string;args:unknown}[]=[];
  const outputs:Record<string,unknown>={listDrives:await mock.drives.list(),listFiles:await mock.files.list({location:{driveId:'demo-files',path:'Archive'},requestId:'read'}),jobSnapshot:await mock.jobs.snapshot(),listAgents:await mock.agents.list(),listTrash:await mock.trash.list('demo-files'),getConfiguration:await mock.configuration.get(),listDiagnostics:await mock.diagnostics.list(),getStatus:await mock.status.get(),reconcileOperation:null,selectNative:null,testHealth:{simulated:false,success:false,message:'Health not configured'}};
  const service=createServiceBackend(bridgeWith(async(action,args)=>{calls.push({action,args});return success(real(outputs[action]));}));
  expect(service.mode).toBe('service');expect((await service.drives.list())[0].simulated).toBe(false);
  expect((await service.files.list({location:{driveId:'demo-files',path:'Archive'},requestId:'read'})).requestId).toBe('read');
  expect((await service.jobs.snapshot()).jobs.every(job=>job.simulated===false)).toBe(true);
  expect((await service.configuration.get()).simulated).toBe(false);expect((await service.agents.list())[0].credentialDisplay).toBe('Stored securely outside renderer');
  await service.trash.list('demo-files');await service.diagnostics.list();expect((await service.status.get()).simulated).toBe(false);await service.operations.reconcile('unknown');await service.selection.select('files');await service.configuration.testHealth();
  expect(calls.find(call=>call.action==='listFiles')?.args).toMatchObject({query:{requestId:'read'}});service.dispose();mock.dispose();
 });
 it('rejects simulated results, malformed envelopes, and leaked schema fields without mock fallback',async()=>{
  const mock=createMockBackend();const drives=await mock.drives.list();
  for(const value of [success(drives),{ok:true,result:real(drives)},success([{...real(drives[0]),token:'secret'}])]){const service=createServiceBackend(bridgeWith(async()=>value));await expect(service.drives.list()).rejects.toMatchObject({detail:{code:'INVALID_RESPONSE'}});service.dispose();}
  const unavailable=createServiceBackend(undefined);await expect(unavailable.drives.list()).rejects.toMatchObject({detail:{code:'UNAVAILABLE'}});expect(await unavailable.status.get()).toMatchObject({backend:'unavailable',stale:true,simulated:false});unavailable.dispose();mock.dispose();
 });
 it('honors a pre-aborted read and discards a late response',async()=>{
  let resolve:(value:unknown)=>void=()=>{};const handler=vi.fn(()=>new Promise(done=>{resolve=done;}));const service=createServiceBackend(bridgeWith(handler));const controller=new AbortController();const pending=service.drives.list({signal:controller.signal});controller.abort();await expect(pending).rejects.toMatchObject({detail:{code:'ABORTED'}});resolve(success([]));await Promise.resolve();await expect(service.drives.list({signal:controller.signal})).rejects.toMatchObject({detail:{code:'ABORTED'}});expect(handler).toHaveBeenCalledTimes(1);service.dispose();
 });
 it('reports event gaps and stops polling when unsubscribed',async()=>{
  vi.useFakeTimers();let count=0;const service=createServiceBackend(bridgeWith(async(action)=>{if(action==='jobEvents'){count++;return success({events:[],cursor:20,gap:true});}return success(null);}),100);const gap=vi.fn();const off=service.jobs.subscribe(vi.fn(),gap);await vi.advanceTimersByTimeAsync(1);expect(gap).toHaveBeenCalledTimes(1);await vi.advanceTimersByTimeAsync(100);expect(count).toBe(2);off();await vi.advanceTimersByTimeAsync(1000);expect(count).toBe(2);service.dispose();
 });
 it('desktop production mode always wins over test/demo renderer settings',()=>{
  window.archiveDesktop=bridgeWith(async()=>success([]));const service=createDependencies();expect(service.mode).toBe('service');service.dispose();window.archiveDesktop={...window.archiveDesktop,mode:'demo'};const demo=createDependencies();expect(demo.mode).toBe('mock');demo.dispose();
 });
});
