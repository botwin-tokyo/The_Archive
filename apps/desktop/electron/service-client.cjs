'use strict';
const fs = require('node:fs');
const { Buffer } = require('node:buffer');
const { URLSearchParams } = require('node:url');
const { randomUUID } = require('node:crypto');
const { z } = require('zod');
const MAX_JSON = 4 * 1024 * 1024;
const MAX_CHUNK = 1024 * 1024;
const id = z.string().min(1).max(256);
const logicalPath = z.string().max(4096).refine(value => !value.startsWith('/') && !value.includes('\\') && !value.includes('\0') && !value.split('/').some(part => part === '.' || part === '..' || part.startsWith('.archive-')) && (value === '' || !value.endsWith('/') && !value.includes('//')));
const name = z.string().min(1).max(255).refine(value => value.trim() && !/[/\\\0]/.test(value) && value !== '.' && value !== '..' && !value.startsWith('.archive-'));
const revision = z.number().int().nonnegative();
const bytes = z.string().regex(/^(0|[1-9]\d*)$/);
const location = z.object({driveId:id,path:logicalPath}).strict();
const ref = location.extend({id,revision}).strict();
const scopes = z.array(z.enum(['read','write','trash','purge','settings','acquire','radio','vault.read','vault.reveal','vault.write','vault.export','skills.read','skills.write','phone_transfer.send','phone_transfer.read','phone_transfer.delete'])).max(16);
const selection = z.object({handle:z.string().regex(/^native:[a-f0-9-]{36}$/),label:z.string().max(255),totalFiles:z.number().int().min(0).max(250000).optional(),totalBytes:bytes.optional(),items:z.array(z.object({relativePath:logicalPath.refine(Boolean),sizeBytes:bytes}).strict()).max(1000),kind:z.enum(['files','folder','download-destination']),simulated:z.literal(false)}).strict();
const request = z.object({requestId:id,kind:z.enum(['new-folder','rename','copy','move','upload','download','trash','restore','purge']),actorId:id.optional(),targets:z.array(ref).max(1000).optional(),destination:location.optional(),name:name.optional(),conflictPolicy:z.enum(['ask','skip','keep-both','replace']).optional(),selection:selection.optional(),confirmation:z.object({kind:z.literal('permanent-delete'),driveId:id,targets:z.array(ref).min(1).max(1000),acknowledged:z.literal(true)}).strict().optional(),recreateParents:z.boolean().optional()}).strict();
const configuration=z.object({revision,driveLabels:z.record(z.string(),z.string().min(1).max(120)),transferConcurrency:z.number().int().min(1).max(4),health:z.object({enabled:z.boolean(),endpoint:z.string().max(500).refine(value=>{if(!value)return true;try{const u=new URL(value);return u.protocol==='wss:'&&!u.username&&!u.password&&!u.search&&!u.hash;}catch{return false;}}),intervalSeconds:z.number().int().min(5).max(3600)}).strict(),appearance:z.object({reducedMotion:z.boolean(),compactRows:z.boolean()}).strict(),restartRequired:z.boolean(),simulated:z.literal(false)}).strict();
const empty=z.object({}).strict();
const animeSeries=z.string().regex(/^[a-z]{2,20}\/[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{1,12}$/);
const ACTIONS=Object.freeze({
 ...require('./vault-actions.cjs'),
 ...require('./skills-actions.cjs'),
 issueVaultKey:[null,z.object({label:z.string().min(1).max(120)}).strict()],
 retryGitBackup:['gitBackup.retry',z.object({requestId:id}).strict()],getGitBackupStatus:['gitBackup.status',empty],configureGitBackup:['gitBackup.configure',z.object({settings:z.object({enabled:z.boolean(),scope:z.enum(['owned','controlled']),branchOverrides:z.record(z.string(),z.string()),retention:z.number().int().min(1).max(365),timezone:z.literal('UTC')}).strict()}).strict()],runGitBackup:['gitBackup.run',z.object({requestId:id}).strict()],reconcileGitBackup:['gitBackup.reconcile',z.object({requestId:id}).strict()],cancelGitBackup:['gitBackup.cancel',empty],previewGitBackup:['gitBackup.preview',empty],
 getAnimeStatus:['anime.status',empty],
 searchAnime:['anime.search',z.object({requestId:id,query:z.string().min(1).max(200),page:z.number().int().min(1).max(50).optional()}).strict()],
 getAnimeSearch:['anime.search.get',z.object({id}).strict()],
 lookupAnimeSeries:['anime.series.lookup',z.object({requestId:id,anilistId:z.number().int().min(1).max(1e9),providerSeriesId:animeSeries.optional()}).strict()],
 getAnimeSeries:['anime.series.get',z.object({id}).strict()],
 getAnimeCover:['anime.cover.get',z.object({anilistId:z.number().int().min(1).max(1e9)}).strict()],
 resolveAnime:['anime.resolve',z.object({requestId:id,seriesId:id,seriesRevision:z.number().int().positive(),episodeIds:z.array(z.string().regex(/^[A-Za-z0-9._-]{1,200}$/)).min(1).max(200),audio:z.enum(['sub','dub']),quality:z.string().regex(/^(best|[1-9][0-9]{2,3}p)$/),subtitles:z.array(z.enum(['en','es','pt','fr','de','it','ru','ar','ja','zh','ko','id','th','vi','pl','tr','hi','ms'])).max(18),destination:location}).strict()],
 getAnimePlan:['anime.plan.get',z.object({id}).strict()],
 enqueueAnime:['anime.enqueue',z.object({requestId:id,planId:id,planRevision:z.number().int().positive()}).strict()],
 reconcileAnime:['anime.reconcile',z.object({requestId:id}).strict()],
 listAnimeJobs:['anime.jobs.list',z.object({cursor:id.optional(),limit:z.number().int().min(1).max(100).optional()}).strict()],
 getAnimeJob:['anime.jobs.get',z.object({id}).strict()],
 controlAnimeJob:['anime.jobs.control',z.object({id,revision:z.number().int().positive(),action:z.enum(['pause','resume','cancel','retry-failed'])}).strict()],
 getVideoStatus:['video.status',empty],
 resolveVideo:['video.resolve',z.object({requestId:id,url:z.string().min(1).max(2048)}).strict()],
 getVideoPlan:['video.plan.get',z.object({id}).strict()],
 enqueueVideo:['video.enqueue',z.object({requestId:id,planId:id,planRevision:revision,choiceId:id,destination:location}).strict()],
 listVideoJobs:['video.jobs.list',z.object({cursor:id.optional(),limit:z.number().int().min(1).max(100).optional()}).strict()],
 getVideoJob:['video.jobs.get',z.object({id}).strict()],
 controlVideoJob:['video.jobs.control',z.object({id,revision,action:z.enum(['pause','resume','cancel','retry-failed'])}).strict()],
 getMusicLyricsStatus:['music.lyrics.status',empty],
 backfillMusicLyrics:['music.lyrics.backfill',z.object({requestId:id}).strict()],
 getMusicArtwork:['music.artwork.get',z.object({id}).strict()],
 getMusicStatus:['music.status',empty],
 searchMusic:['music.search',z.object({requestId:id,query:z.string().min(1).max(300),kind:z.enum(['track','album','artist']),cursor:z.string().max(256).optional()}).strict()],
 getMusicSearch:['music.search.get',z.object({id}).strict()],
 resolveMusic:['music.resolve',z.object({requestId:id,selection:z.object({kind:z.enum(['track','album','artist']),id:z.string().regex(/^[A-Za-z0-9]{22}$/)}).strict()}).strict()],
 getMusicPlan:['music.plan.get',z.object({id}).strict()],
 enqueueMusic:['music.enqueue',z.object({requestId:id,planId:id,planRevision:revision,destination:location}).strict()],
 listMusicJobs:['music.jobs.list',z.object({cursor:id.optional()}).strict()],
 getMusicJob:['music.jobs.get',z.object({id}).strict()],
 controlMusicJob:['music.jobs.control',z.object({id,revision,action:z.enum(['pause','resume','cancel','retry-failed'])}).strict()],

 listDrives:['drives.list',empty],
 listFiles:['files.list',z.object({query:z.object({location,requestId:id,cursor:z.string().max(4096).nullable().optional(),limit:z.number().int().min(1).max(500).optional(),sort:z.enum(['name','modified','type','size']).optional(),direction:z.enum(['asc','desc']).optional(),foldersFirst:z.boolean().optional(),search:z.string().max(255).optional(),scope:z.enum(['directory','recursive']).optional()}).strict()}).strict()],
 previewImage:[null,z.object({ref}).strict()],
 statFile:['files.stat',z.object({ref}).strict()],submitOperation:['operations.submit',z.object({request}).strict()],
 reconcileOperation:['operations.reconcile',z.object({requestId:id}).strict()],jobSnapshot:['jobs.snapshot',empty],
 jobEvents:['jobs.events',z.object({after:revision,limit:z.number().int().min(1).max(200)}).strict()],
 controlJob:['jobs.control',z.object({id,action:z.enum(['pause','resume','cancel','retry'])}).strict()],listTrash:['trash.list',z.object({driveId:id}).strict()],
 getConfiguration:['configuration.get',empty],saveConfiguration:['configuration.save',z.object({draft:configuration}).strict()],testHealth:['configuration.testHealth',empty],listAgents:['agents.list',empty],
 updateAgentScopes:['agents.updateScopes',z.object({id,revision,scopes,allowedRoots:z.array(location).max(1000)}).strict()],revokeAgent:['agents.revoke',z.object({id,revision}).strict()],listDiagnostics:['diagnostics.list',empty],getStatus:['status.get',empty],selectNative:[null,z.object({kind:z.enum(['files','folder','download-destination'])}).strict()],
});
const ERROR_CODES={INTEGRITY_ERROR:'INTEGRITY_FAILED',RESOURCE_EXHAUSTED:'UNAVAILABLE',AUTH_REQUIRED:'PERMISSION_DENIED',UNAUTHENTICATED:'PERMISSION_DENIED',INVALID_ARGUMENT:'INVALID_REQUEST',DRIVE_UNAVAILABLE:'UNAVAILABLE',STATE_UNAVAILABLE:'UNAVAILABLE',INSUFFICIENT_SPACE:'FULL_DISK',QUOTA_EXCEEDED:'FULL_DISK',TRANSFER_INTERRUPTED:'INTERRUPTED',UNSUPPORTED_ENTRY:'UNSUPPORTED',UNSUPPORTED_VERSION:'UNSUPPORTED',PAUSED:'ABORTED',CANCELED:'ABORTED',BUSY:'UNAVAILABLE',INTERNAL_ERROR:'UNAVAILABLE',PARTIAL_OUTCOME:'INTERRUPTED'};
function normalizeCodes(value){if(value&&typeof value==='object'){if(typeof value.code==='string'&&Object.hasOwn(ERROR_CODES,value.code))value.code=ERROR_CODES[value.code];for(const child of Object.values(value))normalizeCodes(child);}return value;}
const CODES=new Set(['UNAVAILABLE','TIMEOUT','PERMISSION_DENIED','PERMISSION_REVOKED','READ_ONLY','FULL_DISK','CONFLICT','STALE_REVISION','INVALID_PATH','NOT_FOUND','SOURCE_CHANGED','INTERRUPTED','INTEGRITY_FAILED','UNSUPPORTED','ABORTED','INVALID_RESPONSE','INVALID_REQUEST','CONFIRMATION_REQUIRED']);
function failure(code,message,retryable=false){const error=new Error(message);error.detail={code,message,retryable,correlationId:`desktop-${randomUUID()}`,recovery:retryable?['retry']:[]};return error;}
function safeFailure(cause){if(cause?.detail&&CODES.has(cause.detail.code))return cause.detail;return failure('UNAVAILABLE','The service request could not complete',true).detail;}
function readServiceConfig(env=process.env){
 const mode=env.ARCHIVE_CONSOLE_MODE||'service';
 if(!['demo','service'].includes(mode))throw failure('INVALID_REQUEST','Invalid desktop mode');
 if(mode==='demo')return {mode,configured:false};
 const endpoint=env.ARCHIVE_SERVICE_ENDPOINT;
 if(!endpoint)return {mode,configured:false};
 let url;try{url=new URL(endpoint);}catch{throw failure('INVALID_REQUEST','Invalid service endpoint');}
 if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash||url.pathname!=='/'||(url.protocol==='http:'&&!['127.0.0.1','[::1]'].includes(url.hostname)))throw failure('INVALID_REQUEST','Service transport must be HTTPS or numeric loopback HTTP, with no path or credentials');
 let token=env.ARCHIVE_SERVICE_TOKEN;
 if(token&&env.ARCHIVE_SERVICE_TOKEN_FILE)throw failure('INVALID_REQUEST','Choose one service credential source');
 if(env.ARCHIVE_SERVICE_TOKEN_FILE){
  const fd=fs.openSync(env.ARCHIVE_SERVICE_TOKEN_FILE,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
  try{const st=fs.fstatSync(fd);if(!st.isFile()||st.size>4096||(st.mode&0o077)!==0||(process.getuid&&st.uid!==process.getuid()))throw failure('PERMISSION_DENIED','Credential file must be private and owned by the desktop user');token=fs.readFileSync(fd,'utf8').trim();}finally{fs.closeSync(fd);}
 }
 if(!token)return {mode,configured:false};
 if(token.length<32||token.length>512||/\s/.test(token))throw failure('INVALID_REQUEST','Invalid service credential format');
 return {mode,configured:true,endpoint:url.origin,token};
}
async function boundedBody(response,limit){
 const length=response.headers.get('content-length');if(length&&Number(length)>limit){await response.body?.cancel();throw failure('INVALID_RESPONSE','Service response exceeds its limit');}
 if(!response.body)return Buffer.alloc(0);
 const reader=response.body.getReader();const chunks=[];let total=0;
 try{while(true){const {done,value}=await reader.read();if(done)break;total+=value.byteLength;if(total>limit){await reader.cancel();throw failure('INVALID_RESPONSE','Service response exceeds its limit');}chunks.push(Buffer.from(value));}}finally{reader.releaseLock();}
 return Buffer.concat(chunks,total);
}
function createServiceClient(config,fetchImpl=globalThis.fetch){
 let inflight=0;
 async function transport(route,options={},binary=false,jsonLimit=MAX_JSON,binaryLimit=MAX_CHUNK){
  if(!config.configured)throw failure('UNAVAILABLE','Service endpoint and owner credential are not configured');
  if(inflight>=32)throw failure('UNAVAILABLE','Too many desktop service requests',true);
  inflight++;
  try{
   const response=await fetchImpl(config.endpoint+route,{...options,headers:{...options.headers,Authorization:`Bearer ${config.token}`},redirect:'error',signal:globalThis.AbortSignal.timeout(20000)});
   const data=await boundedBody(response,binary&&response.ok?binaryLimit:jsonLimit);
   if(binary&&response.ok)return {bytes:data,revision:response.headers.get('x-archive-revision')};
   let envelope;try{envelope=JSON.parse(data.toString('utf8'));}catch{throw failure('INVALID_RESPONSE','Service returned invalid JSON');}
   if(!envelope||typeof envelope.ok!=='boolean')throw failure('INVALID_RESPONSE','Service returned an incompatible envelope');
   if(!envelope.ok){const source=normalizeCodes(envelope.error);const code=CODES.has(source?.code)?source.code:'INVALID_RESPONSE';const err=failure(code,typeof source?.message==='string'&&!source.message.includes(config.token)?source.message.slice(0,500):'Service request was rejected',Boolean(source?.retryable));if(typeof source?.correlationId==='string'&&source.correlationId.length<=256)err.detail.correlationId=source.correlationId;throw err;}
   if(!response.ok)throw failure('UNAVAILABLE','Service transport failed',true);
   return normalizeCodes(envelope.result);
  }catch(cause){if(cause?.detail)throw cause;throw failure(cause?.name==='TimeoutError'?'TIMEOUT':'UNAVAILABLE','The service connection failed',true);}finally{inflight--;}
 }
 return {
  configured:Boolean(config.configured),
  call(capability,args){return transport('/v1/call',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:1,capability,arguments:args})},false,capability==='skills.document'?8*1024*1024:MAX_JSON);},
  downloadSkillFile(input){const value=validateAction('skillsDownload',input);const query=new URLSearchParams({path:value.path,revision:String(value.revision)});return transport(`/skills/v1/skills/${encodeURIComponent(value.id)}/files?${query}`,{},true,MAX_JSON,16*1024*1024);},
  upload(transferId,offset,chunk){if(!id.safeParse(transferId).success||!bytes.safeParse(offset).success||!Buffer.isBuffer(chunk)||chunk.length>MAX_CHUNK)throw failure('INVALID_REQUEST','Invalid bounded upload range');return transport(`/v1/transfers/${encodeURIComponent(transferId)}?offset=${offset}`,{method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:chunk});},
  download(source,offset,length){if(!ref.safeParse(source).success||!bytes.safeParse(offset).success||!Number.isSafeInteger(length)||length<0||length>MAX_CHUNK)throw failure('INVALID_REQUEST','Invalid bounded download range');const query=new URLSearchParams({driveId:source.driveId,path:source.path,revision:String(source.revision),offset,length:String(length)});return transport(`/v1/download?${query}`,{},true);},
 };
}
async function submitPurge(client,request){
 const confirmation=request.confirmation;const targets=request.targets;
 if(!confirmation||!targets?.length||confirmation.acknowledged!==true||targets.some(ref=>ref.driveId!==confirmation.driveId)||JSON.stringify(targets)!==JSON.stringify(confirmation.targets))throw failure('CONFIRMATION_REQUIRED','Acknowledge these exact permanent-deletion targets and revisions');
 const previous=await client.call('operations.reconcile',{requestId:request.requestId});if(previous?.status==='accepted')return previous;
 const grant=await client.call('trash.confirm',{driveId:confirmation.driveId,targets});const grantId=grant?.grantId??grant?.id;if(typeof grantId!=='string'||grantId.length>256)throw failure('INVALID_RESPONSE','The exact-target confirmation grant is invalid');
 return client.call('operations.submit',{request:{...request,confirmation:{...confirmation,grantId}}});
}
function validateAction(action,args){const specification=Object.hasOwn(ACTIONS,action)?ACTIONS[action]:null;if(!specification||Buffer.byteLength(JSON.stringify(args)??'')>256*1024)throw failure('INVALID_REQUEST','Invalid bridge action');const result=specification[1].safeParse(args);if(!result.success)throw failure('INVALID_REQUEST','Invalid bridge arguments');return result.data;}
module.exports={ACTIONS,MAX_CHUNK,submitPurge,createServiceClient,readServiceConfig,validateAction,failure,safeFailure};
