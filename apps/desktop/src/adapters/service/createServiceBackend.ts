import {skillEntrySchema,skillDetailSchema,skillStatusSchema,skillListSchema,skillDocumentSchema,skillReadSchema} from '../../contracts/skills';
import {vaultEntrySchema,vaultSecretSchema,vaultStatusSchema,vaultListSchema,vaultHistorySchema} from '../../contracts/vault';
import {imagePreviewSchema} from '../../contracts';
import {gitStatusSchema,gitRunSchema} from '../../contracts/gitBackup';
import {videoStatusSchema,videoPlanSchema,videoJobSchema,videoJobsSchema} from '../../contracts';
import {animeStatusSchema,animeSearchSchema,animeSeriesSchema,animeCoverSchema,animePlanSchema,animeJobSchema,animeJobsSchema,animeReconcileSchema} from '../../contracts';
import {musicLyricsStatusSchema,musicStatusSchema,musicSearchSchema,musicPlanSchema,musicJobSchema,musicJobsSchema} from '../../contracts';
import { z } from 'zod';
import { agentSummarySchema, appErrorSchema, configurationSchema, diagnosticSchema, directoryPageSchema, directoryQuerySchema, driveSchema, entryRefSchema, entrySchema, idSchema, jobEventSchema, jobSchema, jobSnapshotSchema, operationReceiptSchema, operationRequestSchema, serviceStatusSchema, syntheticSelectionSchema, trashEntrySchema, ContractError, domainError } from '../../contracts';
import type { BackendPorts, JobEvent, ReadOptions, ServiceStatus } from '../../contracts';

export const SERVICE_ACTIONS = ['skillsStatus','skillsList','skillsGet','skillsDocument','skillsFileRead','skillsDownload','skillsCopy','skillsOpenLink','skillsTrash','skillsRestore','issueSkillsKey','issueVaultKey','vaultStatus','vaultHelp','vaultEntriesList','vaultEntriesGet','vaultEntriesReveal','vaultEntriesCreate','vaultEntriesUpdate','vaultEntriesTrash','vaultEntriesRestore','vaultEntriesHistory','vaultEntriesRestoreVersion','vaultPasswordsGenerate','vaultRequestsReconcile','vaultBackupExport','vaultBackupImport','copyVaultSecret','retryGitBackup','getGitBackupStatus','configureGitBackup','runGitBackup','reconcileGitBackup','cancelGitBackup','previewGitBackup','getAnimeStatus','searchAnime','getAnimeSearch','lookupAnimeSeries','getAnimeSeries','getAnimeCover','resolveAnime','getAnimePlan','enqueueAnime','reconcileAnime','listAnimeJobs','getAnimeJob','controlAnimeJob','getVideoStatus','resolveVideo','getVideoPlan','enqueueVideo','listVideoJobs','getVideoJob','controlVideoJob','getMusicLyricsStatus','backfillMusicLyrics','getMusicArtwork','getMusicStatus','searchMusic','getMusicSearch','resolveMusic','getMusicPlan','enqueueMusic','listMusicJobs','getMusicJob','controlMusicJob','listDrives','listFiles','statFile','previewImage','submitOperation','reconcileOperation','jobSnapshot','jobEvents','controlJob','listTrash','getConfiguration','saveConfiguration','testHealth','listAgents','updateAgentScopes','revokeAgent','listDiagnostics','getStatus','selectNative'] as const;
export type ServiceAction = typeof SERVICE_ACTIONS[number];
export interface DesktopBridge {
 readonly mode: 'demo' | 'service';
 getVersion(): Promise<{schemaVersion:1;appVersion:string;mode:'demo'|'service';configured:boolean}>;
 backend: Readonly<Record<ServiceAction, (arguments_: unknown) => Promise<unknown>>>;
}
declare global { interface Window { archiveDesktop?: DesktopBridge } }
const error = (code: 'UNAVAILABLE'|'INVALID_RESPONSE'|'ABORTED', message: string) => domainError(code,message,true,'desktop-service',['retry']);
const envelopeSchema = z.discriminatedUnion('ok', [z.object({version:z.literal(1),mode:z.literal('service'),ok:z.literal(true),result:z.unknown()}).strict(),z.object({version:z.literal(1),mode:z.literal('service'),ok:z.literal(false),error:appErrorSchema}).strict()]);
function rejectSimulated(value: unknown): void {
 if (value && typeof value === 'object') {
  if ('simulated' in value && value.simulated !== false) throw error('INVALID_RESPONSE','Service returned a simulated result');
  for (const item of Object.values(value)) rejectSimulated(item);
 }
}
/** IPC only. The renderer never learns service credentials, addresses or OS paths. */
export function createServiceBackend(bridge: DesktopBridge | undefined, pollMs = 1500): BackendPorts & {mode:'service';dispose():void} {
 let disposed = false, statusTimer: ReturnType<typeof setTimeout>|undefined, jobTimer: ReturnType<typeof setTimeout>|undefined, cursor = 0, statusGeneration=0, jobGeneration=0;
 let lastStatus: ServiceStatus = {schemaVersion:1,backend:'unavailable',freshnessAt:new Date().toISOString(),api:'not-configured',mcp:'not-configured',health:'not-configured',authenticatedSessions:0,stale:true,simulated:false};
 const statusListeners = new Set<(status:ServiceStatus)=>void>();
 const jobListeners = new Map<(event:JobEvent)=>void, (()=>void)|undefined>();
 async function call<T>(action: ServiceAction, arguments_: unknown, schema: z.ZodType<T>, options?: ReadOptions): Promise<T> {
  if (disposed || options?.signal?.aborted) throw error('ABORTED','This read was canceled');
  if (!bridge || bridge.mode !== 'service') throw error('UNAVAILABLE','The desktop service bridge is not configured. Open the desktop application or select Demo explicitly.');
  let onAbort: (()=>void)|undefined;
  try {
   const pending = bridge.backend[action](arguments_);
   const response = options?.signal ? await Promise.race([pending,new Promise<never>((_resolve,reject)=>{onAbort=()=>reject(error('ABORTED','This read was canceled'));options.signal!.addEventListener('abort',onAbort,{once:true});})]) : await pending;
   if (disposed || options?.signal?.aborted) throw error('ABORTED','This read was canceled');
   const envelope = envelopeSchema.safeParse(response);
   if (!envelope.success) throw error('INVALID_RESPONSE','The service response envelope is incompatible');
   if (!envelope.data.ok) throw new ContractError(envelope.data.error);
   rejectSimulated(envelope.data.result);
   const parsed = schema.safeParse(envelope.data.result);
   if (!parsed.success) throw error('INVALID_RESPONSE','The service result could not be validated');
   return parsed.data;
  } catch (cause) { if(cause instanceof ContractError)throw cause;throw error('UNAVAILABLE','The service connection failed. No demo fallback was used.'); }
  finally { if(onAbort)options?.signal?.removeEventListener('abort',onAbort); }
 }
 const publishStatus = (status:ServiceStatus) => {lastStatus=status;statusListeners.forEach(listener=>listener(status));};
 const unavailable = () => publishStatus({...lastStatus,backend:'unavailable',stale:true});
 async function pollStatus(generation:number) {
  if(disposed||!statusListeners.size||generation!==statusGeneration)return;
  try {const status=await call('getStatus',{},serviceStatusSchema);if(generation===statusGeneration&&!disposed)publishStatus(status);} catch {if(generation===statusGeneration&&!disposed)unavailable();}
  if(!disposed&&statusListeners.size&&generation===statusGeneration)statusTimer=setTimeout(()=>void pollStatus(generation),pollMs);
 }
 const eventPageSchema=z.object({events:z.array(jobEventSchema).max(200),cursor:z.number().int().nonnegative(),gap:z.boolean()}).strict();
 async function pollJobs(generation:number) {
  if(disposed||!jobListeners.size||generation!==jobGeneration)return;
  try {
   const page=await call('jobEvents',{after:cursor,limit:200},eventPageSchema);if(generation!==jobGeneration||disposed)return;
   if(page.gap){jobListeners.forEach(onGap=>onGap?.());cursor=page.cursor;}
   else for(const event of page.events){if(event.cursor<=cursor)continue;if(event.cursor!==cursor+1){jobListeners.forEach(onGap=>onGap?.());cursor=page.cursor;break;}cursor=event.cursor;jobListeners.forEach((_onGap,listener)=>listener(event));}
  } catch {if(generation===jobGeneration&&!disposed){unavailable();jobListeners.forEach(onGap=>onGap?.());}}
  if(!disposed&&jobListeners.size&&generation===jobGeneration)jobTimer=setTimeout(()=>void pollJobs(generation),pollMs);
 }
 return {
  mode:'service',
  gitBackup:{retry:requestId=>call('retryGitBackup',{requestId},gitRunSchema),status:()=>call('getGitBackupStatus',{},gitStatusSchema),configure:settings=>call('configureGitBackup',{settings},gitStatusSchema),run:requestId=>call('runGitBackup',{requestId},gitRunSchema),reconcile:requestId=>call('reconcileGitBackup',{requestId},gitRunSchema.nullable()),cancel:()=>call('cancelGitBackup',{},gitStatusSchema),preview:()=>call('previewGitBackup',{},gitStatusSchema)},
  anime:{status:()=>call('getAnimeStatus',{},animeStatusSchema),search:args=>call('searchAnime',args,animeSearchSchema),getSearch:id=>call('getAnimeSearch',{id},animeSearchSchema),
   lookup:args=>call('lookupAnimeSeries',args,animeSeriesSchema),getSeries:id=>call('getAnimeSeries',{id},animeSeriesSchema),cover:anilistId=>call('getAnimeCover',{anilistId},animeCoverSchema),
   resolve:args=>call('resolveAnime',args,animePlanSchema),getPlan:id=>call('getAnimePlan',{id},animePlanSchema),enqueue:args=>call('enqueueAnime',args,animeJobSchema),
   reconcile:requestId=>call('reconcileAnime',{requestId},animeReconcileSchema),listJobs:args=>call('listAnimeJobs',args??{},animeJobsSchema),getJob:id=>call('getAnimeJob',{id},animeJobSchema),control:args=>call('controlAnimeJob',args,animeJobSchema)},
  video:{status:()=>call('getVideoStatus',{},videoStatusSchema),resolve:args=>call('resolveVideo',args,videoPlanSchema),getPlan:id=>call('getVideoPlan',{id},videoPlanSchema),enqueue:args=>call('enqueueVideo',args,videoJobSchema),listJobs:args=>call('listVideoJobs',args??{},videoJobsSchema),getJob:id=>call('getVideoJob',{id},videoJobSchema),control:args=>call('controlVideoJob',args,videoJobSchema)},
  skills:{status:()=>call('skillsStatus',{},skillStatusSchema),list:args=>call('skillsList',args,skillListSchema),get:(id,revision)=>call('skillsGet',{id,...(revision===undefined?{}:{revision})},skillDetailSchema),document:(id,revision)=>call('skillsDocument',{id,...(revision===undefined?{}:{revision})},skillDocumentSchema),fileRead:(id,path,revision)=>call('skillsFileRead',{id,path,revision},skillReadSchema),download:(id,path,revision)=>call('skillsDownload',{id,path,revision},z.object({saved:z.boolean()}).strict()),copy:(id,revision)=>call('skillsCopy',{id,revision},z.object({copied:z.boolean()}).strict()),openLink:url=>call('skillsOpenLink',{url},z.object({opened:z.boolean()}).strict()),trash:args=>call('skillsTrash',args,skillEntrySchema),restore:args=>call('skillsRestore',args,skillEntrySchema),issueKey:label=>call('issueSkillsKey',{label},z.object({id:z.string(),secret:z.string()}).strict())},
  vault:{issueKey:label=>call('issueVaultKey',{label},z.object({id:z.string(),secret:z.string()}).strict()),status:()=>call('vaultStatus',{},vaultStatusSchema),list:args=>call('vaultEntriesList',args,vaultListSchema),reveal:(id,version)=>call('vaultEntriesReveal',{id,...(version===undefined?{}:{version})},vaultSecretSchema),create:args=>call('vaultEntriesCreate',args,vaultEntrySchema),update:args=>call('vaultEntriesUpdate',args,vaultEntrySchema),trash:args=>call('vaultEntriesTrash',args,vaultEntrySchema),restore:args=>call('vaultEntriesRestore',args,vaultEntrySchema),restoreVersion:args=>call('vaultEntriesRestoreVersion',args,vaultEntrySchema),history:(id,cursor)=>call('vaultEntriesHistory',{id,...(cursor?{cursor}:{})},vaultHistorySchema),generate:length=>call('vaultPasswordsGenerate',{length},z.object({password:z.string()}).strict()),copy:(id,field)=>call('copyVaultSecret',{id,field},z.object({copied:z.boolean()}).strict())},
  music:{lyricsStatus:()=>call('getMusicLyricsStatus',{},musicLyricsStatusSchema),backfillLyrics:args=>call('backfillMusicLyrics',args,musicLyricsStatusSchema),artwork:id=>call('getMusicArtwork',{id},z.object({dataUrl:z.string().max(131072).regex(/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/).nullable()}).strict()),status:()=>call('getMusicStatus',{},musicStatusSchema),search:args=>call('searchMusic',args,musicSearchSchema),getSearch:id=>call('getMusicSearch',{id},musicSearchSchema),resolve:args=>call('resolveMusic',args,musicPlanSchema),getPlan:id=>call('getMusicPlan',{id},musicPlanSchema),enqueue:args=>call('enqueueMusic',args,musicJobSchema),listJobs:args=>call('listMusicJobs',args??{},musicJobsSchema),getJob:id=>call('getMusicJob',{id},musicJobSchema),control:args=>call('controlMusicJob',args,musicJobSchema)},
  drives:{list:options=>call('listDrives',{},z.array(driveSchema).max(64),options)},
  files:{previewImage:(ref,options)=>call('previewImage',{ref:entryRefSchema.parse(ref)},imagePreviewSchema,options),list:(query,options)=>call('listFiles',{query:directoryQuerySchema.parse(query)},directoryPageSchema,options),stat:(ref,options)=>call('statFile',{ref:entryRefSchema.parse(ref)},entrySchema,options)},
  operations:{submit:request=>call('submitOperation',{request:operationRequestSchema.parse(request)},operationReceiptSchema),reconcile:requestId=>call('reconcileOperation',{requestId:idSchema.parse(requestId)},operationReceiptSchema.nullable())},
  jobs:{snapshot:async options=>{const snapshot=await call('jobSnapshot',{},jobSnapshotSchema,options);cursor=Math.max(cursor,snapshot.cursor);return snapshot;},subscribe:(listener,onGap)=>{jobListeners.set(listener,onGap);if(jobListeners.size===1)void pollJobs(++jobGeneration);return()=>{jobListeners.delete(listener);if(!jobListeners.size){jobGeneration++;clearTimeout(jobTimer);}};},control:(id,action)=>call('controlJob',{id:idSchema.parse(id),action},jobSchema)},
  trash:{list:(driveId,options)=>call('listTrash',{driveId:idSchema.parse(driveId)},z.array(trashEntrySchema).max(1000),options)},
  configuration:{get:options=>call('getConfiguration',{},configurationSchema,options),save:draft=>call('saveConfiguration',{draft:configurationSchema.parse(draft)},configurationSchema),testHealth:()=>call('testHealth',{},z.object({simulated:z.literal(false),success:z.boolean(),message:z.string().max(500)}).strict())},
  agents:{list:options=>call('listAgents',{},z.array(agentSummarySchema).max(1000),options),updateScopes:(id,revision,scopes,allowedRoots)=>call('updateAgentScopes',{id,revision,scopes,allowedRoots},agentSummarySchema),revoke:(id,revision)=>call('revokeAgent',{id,revision},agentSummarySchema)},
  diagnostics:{list:options=>call('listDiagnostics',{},z.array(diagnosticSchema).max(1000),options)},
  selection:{select:kind=>call('selectNative',{kind},syntheticSelectionSchema.nullable())},
  status:{get:async options=>{try{const status=await call('getStatus',{},serviceStatusSchema,options);lastStatus=status;return status;}catch(cause){if(options?.signal?.aborted)throw cause;unavailable();return lastStatus;}},subscribe:listener=>{statusListeners.add(listener);if(statusListeners.size===1)void pollStatus(++statusGeneration);return()=>{statusListeners.delete(listener);if(!statusListeners.size){statusGeneration++;clearTimeout(statusTimer);}};}},
  dispose(){disposed=true;clearTimeout(jobTimer);clearTimeout(statusTimer);statusListeners.clear();jobListeners.clear();},
 };
}
