'use strict';
const fs=require('node:fs');
const fsp=require('node:fs/promises');
const path=require('node:path');
const {Buffer}=require('node:buffer');
const {randomUUID,createHash}=require('node:crypto');
const {MAX_CHUNK,failure,validateAction}=require('./service-client.cjs');
const FLAGS=fs.constants;
const O_PATH=0x200000; // Linux open without device/FIFO side effects; verified inode is reopened via proc.
const MAX_FOLDER_ITEMS=250000;
const MAX_ITEMS=1000,MAX_HANDLES=16,TTL=30*60*1000;
const validName=value=>typeof value==='string'&&value.length>0&&value.length<=255&&value.trim()&&!/[/\\\0]/.test(value)&&value!=='.'&&value!=='..'&&!value.startsWith('.archive-');
const same=(a,b)=>a.dev===b.dev&&a.ino===b.ino&&a.size===b.size&&a.mtimeNs===b.mtimeNs;
const beneath=(directory,name)=>`/proc/self/fd/${directory.fd}/${name}`;
function createNativeSelections({dialog,getWindow,client,folderOwners,now=()=>Date.now(),retryDelay=ms=>new Promise(resolve=>setTimeout(resolve,ms))}){
 const handles=new Map(),receipts=new Map(),running=new Map(),resumable=new Map();let disposed=false;
 // Replay only operations with durable idempotency or read-only semantics.
 // In particular, never replay jobs.control: retry can create a new job.
 const transport=client;
 const replayable=new Set(['transfers.query','transfers.initiate','transfers.finalize','transfers.folderStatus','transfers.folderAck','transfers.folderFinish','jobs.get']);
 async function reconnect(operation,beforeReplay){
  for(let attempt=0;;attempt++){
   if(disposed)throw failure('ABORTED','Desktop closed');
   try{if(attempt&&beforeReplay)await beforeReplay();return await operation();}catch(cause){
    if(!['TIMEOUT','UNAVAILABLE'].includes(cause.detail?.code)||!cause.detail?.retryable)throw cause;
    await retryDelay(Math.min(10000,500*2**Math.min(attempt,5)));
   }
  }
 }
 async function awaitWriter(id){
  const transfer=await transport.call('transfers.query',{transferId:id});
  const job=await transport.call('jobs.get',{id:transfer.jobId});
  if(['preparing','running','verifying','committing','pausing','canceling'].includes(job.state))throw failure('UNAVAILABLE','Waiting for the previous transfer request to settle',true);
 }
 client={...transport,
  call:(capability,args)=>replayable.has(capability)||(capability==='operations.submit'&&args.request?.kind==='new-folder')?reconnect(()=>transport.call(capability,args),capability==='transfers.finalize'?()=>awaitWriter(args.transferId):undefined):transport.call(capability,args),
  // The service acknowledges an identical last range after a lost response.
  upload:(id,offset,bytes)=>reconnect(()=>transport.upload(id,offset,bytes),()=>awaitWriter(id)),
 };
 const close=async record=>{for(const [id,item] of resumable)if(item.record===record)resumable.delete(id);for(const partial of record.partials||[]){await partial.output.close().catch(()=>{});await fsp.unlink(beneath(partial.target.directory,partial.temp)).catch(()=>{});await partial.target.close().catch(()=>{});}for(const handle of record.opened)await handle.close().catch(()=>{});};
 async function expire(){for(const [key,record]of handles)if(!record.busy&&record.expires<=now()){handles.delete(key);await close(record);}}
 async function select(kind,picker={}){
  if(disposed)throw failure('ABORTED','The native selection manager is closed');
  if(process.platform!=='linux')throw failure('UNSUPPORTED','Native streaming currently requires Linux descriptor-relative access');
  await expire();if(handles.size>=MAX_HANDLES)throw failure('UNAVAILABLE','Too many native selections; reopen the desktop to clear them');
  const result=await dialog.showOpenDialog(getWindow(),{title:picker.title||(kind==='download-destination'?'Choose download destination':kind==='folder'?'Choose upload folder':'Choose upload files'),...(picker.buttonLabel?{buttonLabel:picker.buttonLabel}:{}),...(picker.message?{message:picker.message}:{}),properties:kind==='files'?(picker.single?['openFile']:['openFile','multiSelections']):['openDirectory'],dontAddToRecent:true});
  if(result.canceled||!result.filePaths.length)return null;
  if(picker.single&&result.filePaths.length!==1)throw failure('INVALID_REQUEST','Choose exactly one original source file');
  if(result.filePaths.length>MAX_ITEMS)throw failure('INVALID_REQUEST','Choose at most 1000 files');
  const record={kind,opened:[],files:[],directories:[],expires:now()+TTL,busy:false,label:'',destination:null};
  async function openDir(filename){const handle=await fsp.open(filename,FLAGS.O_RDONLY|FLAGS.O_DIRECTORY|FLAGS.O_NOFOLLOW);record.opened.push(handle);if(record.opened.length>MAX_ITEMS+64)throw failure('INVALID_REQUEST','Selection exceeds the native descriptor limit');return handle;}
  async function addFile(filename,relativePath){
   if(record.kind==='folder'){if(record.files.length+[...handles.values()].reduce((n,r)=>n+r.files.length,0)>=MAX_FOLDER_ITEMS)throw failure('INVALID_REQUEST','Close previous folder selections before selecting more than 250,000 files');const metadata=await fsp.open(filename,O_PATH|FLAGS.O_NOFOLLOW);try{const stat=await metadata.stat({bigint:true});if(!stat.isFile())throw failure('UNSUPPORTED','Only regular files may be uploaded');record.files.push({relativePath,stat,sizeBytes:String(stat.size)});}finally{await metadata.close();}return;}
   if(record.files.length+[...handles.values()].reduce((sum,item)=>sum+item.files.length,0)>=MAX_ITEMS)throw failure('INVALID_REQUEST','Selection exceeds 1000 files');
   const metadata=await fsp.open(filename,O_PATH|FLAGS.O_NOFOLLOW);let handle;
   try{const stat=await metadata.stat({bigint:true});if(!stat.isFile())throw failure('UNSUPPORTED','Only regular files may be uploaded');handle=await fsp.open(`/proc/self/fd/${metadata.fd}`,FLAGS.O_RDONLY|FLAGS.O_NONBLOCK);const actual=await handle.stat({bigint:true});if(!same(stat,actual))throw failure('SOURCE_CHANGED','Selected source changed before it could be opened');record.opened.push(handle);record.files.push({handle,relativePath,stat,sizeBytes:String(stat.size)});handle=null;}finally{await metadata.close();if(handle)await handle.close();}
  }
  async function walk(dir,relative,depth){if(depth>64)throw failure('INVALID_REQUEST','Selection is too deeply nested');const names=await fsp.opendir(`/proc/self/fd/${dir.fd}`,{bufferSize:32});for await(const entry of names){if(record.files.length+record.directories.length>=(record.kind==='folder'?MAX_FOLDER_ITEMS:MAX_ITEMS))throw failure('INVALID_REQUEST','Folder selection exceeds the 250,000-entry safety limit');if(!validName(entry.name))throw failure('INVALID_PATH','Selection contains a reserved or invalid name');const child=relative?`${relative}/${entry.name}`:entry.name;if(entry.isSymbolicLink()||(!entry.isDirectory()&&!entry.isFile()))throw failure('UNSUPPORTED','Links and special files are not followed');if(entry.isDirectory()){record.directories.push(child);const handle=await openDir(beneath(dir,entry.name));try{await walk(handle,child,depth+1);}finally{await handle.close();record.opened.splice(record.opened.indexOf(handle),1);}}else await addFile(beneath(dir,entry.name),child);}}
  try{
   if(kind==='download-destination'){record.destination=await openDir(result.filePaths[0]);record.label=path.basename(result.filePaths[0])||'Selected folder';}
   else if(kind==='folder'){const root=path.basename(result.filePaths[0]);if(!validName(root))throw failure('INVALID_PATH','Invalid selected folder name');const dir=await openDir(result.filePaths[0]);record.label=root;record.root=dir;record.directories.push(root);await walk(dir,root,0);record.files.sort((a,b)=>a.relativePath<b.relativePath?-1:a.relativePath>b.relativePath?1:0);record.directories.sort();record.manifestDigest=createHash('sha256').update(JSON.stringify({directories:record.directories,files:record.files.map(f=>[f.relativePath,String(f.stat.dev),String(f.stat.ino),f.sizeBytes,String(f.stat.mtimeNs)])})).digest('hex');}
   else {const names=new Set();for(const filename of result.filePaths){const name=path.basename(filename);if(!validName(name)||names.has(name))throw failure('CONFLICT','Selected files must have valid, distinct names');names.add(name);await addFile(filename,name);}record.label=`${record.files.length} selected files`;}
   const key=`native:${randomUUID()}`;const selection={handle:key,label:record.label,kind,simulated:false,items:record.files.slice(0,kind==='folder'?100:MAX_ITEMS).map(file=>({relativePath:file.relativePath,sizeBytes:file.sizeBytes})),...(kind==='folder'?{totalFiles:record.files.length,totalBytes:String(record.files.reduce((n,f)=>n+BigInt(f.sizeBytes),0n))}:{})};if(Buffer.byteLength(JSON.stringify(selection))>256*1024)throw failure('INVALID_REQUEST','Selected metadata exceeds the bridge limit');handles.set(key,record);return selection;
  }catch(cause){await close(record);if(cause.detail)throw cause;throw failure('PERMISSION_DENIED','The selected files could not be opened safely');}
 }
 function selectionFor(request){const record=handles.get(request.selection?.handle);if(!record||record.expires<=now()||record.kind!==request.selection.kind)throw failure('INVALID_REQUEST','Native selection expired. Choose the files again.');if(record.busy)throw failure('CONFLICT','This native selection is already in use');if(request.kind==='upload'&&record.kind==='download-destination'||request.kind==='download'&&record.kind!=='download-destination')throw failure('INVALID_REQUEST','Wrong native selection kind');return record;}
 async function awaitJob(jobId){for(;;){if(disposed)throw failure('INTERRUPTED','Desktop closed');const job=await client.call('jobs.get',{id:jobId});if(job?.state==='completed')return job;if(!job||['failed','canceled','needs-decision'].includes(job.state))throw failure(job?.error?.code||'CONFLICT',job?.error?.message||'Folder preparation needs a decision');await new Promise(resolve=>setTimeout(resolve,200));}}
 async function ensureDirectories(request,record,folderId){
  let index=0;const jobs=[];
  for(const directory of record.directories){if(disposed)throw failure('INTERRUPTED','Desktop closed');if(folderId){const status=await client.call('transfers.folderStatus',{folderId});if(['paused','pausing','canceled','canceling'].includes(status.state))throw failure('ABORTED','Folder upload stopped');}const segments=directory.split('/');const name=segments.pop();const destination={driveId:request.destination.driveId,path:[request.destination.path,...segments].filter(Boolean).join('/')};const outcome=await client.call('operations.submit',{request:{requestId:`${request.requestId}:directory:${index++}`,kind:'new-folder',destination,name,conflictPolicy:'ask'}});if(outcome.status!=='accepted')throw failure('CONFLICT','An upload folder already exists or could not be created. Choose another destination.');jobs.push(...outcome.jobIds);await awaitJob(outcome.jobIds[0]);}return jobs;
 }
 async function hashSource(file){
  if(!same(file.stat,await file.handle.stat({bigint:true})))throw failure('SOURCE_CHANGED','Selected source changed before checksum verification');
  const buffer=Buffer.allocUnsafe(MAX_CHUNK),digest=createHash('sha256');let offset=0n;
  while(offset<file.stat.size){if(disposed)throw failure('INTERRUPTED','Desktop closed before checksum verification');const length=Number(file.stat.size-offset>BigInt(MAX_CHUNK)?BigInt(MAX_CHUNK):file.stat.size-offset);const result=await file.handle.read(buffer,0,length,offset);if(result.bytesRead!==length)throw failure('SOURCE_CHANGED','Selected source changed during checksum verification');digest.update(buffer.subarray(0,length));offset+=BigInt(length);}
  if(!same(file.stat,await file.handle.stat({bigint:true})))throw failure('SOURCE_CHANGED','Selected source changed during checksum verification');
  return digest.digest('hex');
 }
 async function streamUpload(file,transferId){
  let current=await client.call('transfers.query',{transferId});let offset=BigInt(current.offset);const buffer=Buffer.allocUnsafe(MAX_CHUNK);const digest=createHash('sha256');let hashed=0n;
  if(offset<0n||offset>file.stat.size)throw failure('INVALID_RESPONSE','Service returned an invalid upload offset');
  while(hashed<file.stat.size){if(disposed)return;const next=Number(file.stat.size-hashed>BigInt(MAX_CHUNK)?BigInt(MAX_CHUNK):file.stat.size-hashed);const read=await file.handle.read(buffer,0,next,hashed);if(read.bytesRead!==next)throw failure('SOURCE_CHANGED','Selected source changed during upload');digest.update(buffer.subarray(0,read.bytesRead));if(hashed+BigInt(next)>offset){const start=Number(offset>hashed?offset-hashed:0n);const range=buffer.subarray(start,next);const response=await client.upload(transferId,String(offset),range);const acknowledged=BigInt(response.offset);if(acknowledged!==offset+BigInt(range.length))throw failure('INVALID_RESPONSE','Service returned an invalid upload acknowledgement');offset=acknowledged;}hashed+=BigInt(next);}
  if(!same(file.stat,await file.handle.stat({bigint:true})))throw failure('SOURCE_CHANGED','Selected source changed during upload');
  const actual=digest.digest('hex');if(file.sha256&&actual!==file.sha256)throw failure('SOURCE_CHANGED','Selected source no longer matches the recorded upload checksum');
  const result=await client.call('transfers.finalize',{transferId});if(result.verification?.algorithm!=='sha256'||result.verification.digest!==actual)throw failure('INTEGRITY_FAILED','Upload checksum did not match');
 }
 async function submitUpload(request,record){
  if(!request.destination)throw failure('INVALID_REQUEST','Choose a destination');
  if(record.kind==='folder')return submitFolder(request,record);
  if(!record.files.length){const directoryJobs=await ensureDirectories(request,record);record.busy=false;if(!directoryJobs.length)throw failure('INVALID_REQUEST','No files or folders selected');return {status:'accepted',requestId:request.requestId,jobIds:directoryJobs,simulated:false};}
  for(const file of record.files)file.sha256=await hashSource(file);
  const files=record.files.map(file=>{const parts=file.relativePath.split('/');const name=parts.pop();return {destination:{driveId:request.destination.driveId,path:[request.destination.path,...parts].filter(Boolean).join('/')},name,sizeBytes:file.sizeBytes,sha256:file.sha256,conflictPolicy:request.conflictPolicy||'ask'};});
  const sessions=files.length===1?[await client.call('transfers.initiate',{...files[0],requestId:request.requestId,parentRequestId:request.requestId})]:(await client.call('transfers.batchInitiate',{requestId:request.requestId,files})).sessions;
  if(!Array.isArray(sessions)||sessions.length!==files.length)throw failure('INVALID_RESPONSE','Service returned an invalid batch reservation');
  const transfers=sessions.map((transfer,index)=>({transfer,file:record.files[index]}));
  const receipt={status:'accepted',requestId:request.requestId,jobIds:transfers.map(item=>item.transfer.jobId),simulated:false};
  // All byte jobs are durably accepted before local streaming begins. Folder preparation failures are attributed to those jobs.
  let preparation;
  const prepare=()=>preparation??=(ensureDirectories(request,record));
  for(const item of transfers)resumable.set(item.transfer.jobId,{record,transferId:item.transfer.transferId,task:async()=>{await prepare();return streamUpload(item.file,item.transfer.transferId);}});
  void run(request.requestId,record,async()=>{for(const item of transfers)await runTransfer(item.transfer.jobId);});
  return receipt;
 }
 async function openSelected(record,file){
  const parts=file.relativePath.split('/').slice(1),opened=[];let directory=record.root;
  try{for(const part of parts.slice(0,-1)){const next=await fsp.open(beneath(directory,part),FLAGS.O_RDONLY|FLAGS.O_DIRECTORY|FLAGS.O_NOFOLLOW);opened.push(next);directory=next;}
   const handle=await fsp.open(beneath(directory,parts.at(-1)),FLAGS.O_RDONLY|FLAGS.O_NONBLOCK|FLAGS.O_NOFOLLOW);opened.push(handle);
   if(!same(file.stat,await handle.stat({bigint:true})))throw failure('SOURCE_CHANGED','Selected source changed; the original folder must be reselected');
   return {...file,handle,close:async()=>{for(const h of opened.reverse())await h.close();}};
  }catch(cause){for(const h of opened.reverse())await h.close().catch(()=>{});throw cause.detail?cause:failure('SOURCE_CHANGED','Selected external folder is unavailable or changed');}
 }
 function attachFolder(request,record,folderId){
  const task=async()=>{
   let status=await client.call('transfers.folderStatus',{folderId});
   if(status.state==='completed')return;
   if(['paused','pausing','canceled','canceling'].includes(status.state))throw failure('ABORTED','Folder upload stopped');
   if(!record.prepared){await ensureDirectories(request,record,folderId);record.prepared=true;}
   for(let index=status.completedFiles;index<record.files.length;index++){
    if(disposed)throw failure('INTERRUPTED','Desktop closed; reselect the folder to resume');
    status=await client.call('transfers.folderStatus',{folderId});
    if(['paused','pausing','canceled','canceling'].includes(status.state))throw failure('ABORTED','Folder upload stopped');
    const file=await openSelected(record,record.files[index]);
    try{file.sha256=await hashSource(file);const parts=file.relativePath.split('/'),name=parts.pop();
     const transfer=await client.call('transfers.initiate',{requestId:`${request.requestId}:file:${index}`,parentRequestId:request.requestId,folderId,destination:{driveId:request.destination.driveId,path:[request.destination.path,...parts].filter(Boolean).join('/')},name,sizeBytes:file.sizeBytes,sha256:file.sha256,conflictPolicy:'ask'});
     if(transfer.state!=='completed')await streamUpload(file,transfer.transferId);
     await client.call('transfers.folderAck',{folderId,transferId:transfer.transferId});
    }finally{await file.close();}
   }
   await client.call('transfers.folderFinish',{folderId});
  };
  const ownedTask=async()=>{
   const release=folderOwners?await folderOwners.register(folderId):undefined;
   if(folderOwners&&!release)return;
   try{await task();}finally{await release?.();}
  };
  resumable.set(folderId,{record,folderId,task:ownedTask});void run(request.requestId,record,()=>runTransfer(folderId));
 }
 async function submitFolder(request,record){
  const receipt=await client.call('transfers.folderBegin',{requestId:request.requestId,destination:request.destination,name:record.label,fileCount:record.files.length,sizeBytes:String(record.files.reduce((n,f)=>n+BigInt(f.sizeBytes),0n)),manifestDigest:record.manifestDigest});
  if(receipt.status!=='accepted'||typeof receipt.folderId!=='string')throw failure('INVALID_RESPONSE','Folder acceptance response was invalid');
  attachFolder(request,record,receipt.folderId);
  return {status:'accepted',requestId:request.requestId,jobIds:receipt.jobIds,simulated:false};
 }
 async function destinationDirectory(root,parts){let directory=root;const opened=[];try{for(const part of parts){if(!validName(part))throw failure('INVALID_PATH','Invalid download directory');try{await fsp.mkdir(beneath(directory,part),{mode:0o700});}catch(cause){if(cause.code!=='EEXIST')throw cause;}const next=await fsp.open(beneath(directory,part),FLAGS.O_RDONLY|FLAGS.O_DIRECTORY|FLAGS.O_NOFOLLOW);opened.push(next);directory=next;}return {directory,close:async()=>{for(const handle of opened)await handle.close();}};}catch(cause){for(const handle of opened)await handle.close();throw cause;}}
 async function streamDownload(request,record,entry,relativePath,transfer){
  let state=transfer.nativeState;
  if(!state){
   const parts=relativePath.split('/');const original=parts.pop();if(!validName(original))throw failure('INVALID_PATH','Invalid download name');
   const target=await destinationDirectory(record.destination,parts);const temp=`.archive-download-${randomUUID()}`;
   try{const output=await fsp.open(beneath(target.directory,temp),FLAGS.O_RDWR|FLAGS.O_CREAT|FLAGS.O_EXCL|FLAGS.O_NOFOLLOW,0o600);state={target,temp,output,original,offset:0n,total:BigInt(transfer.sizeBytes),published:false};transfer.nativeState=state;record.partials??=[];record.partials.push(state);}catch(cause){await target.close();throw cause;}
  }
  const current=await client.call('transfers.query',{transferId:transfer.transferId});
  if(BigInt(current.offset)>state.offset)throw failure('INTEGRITY_FAILED','Native download partial is behind the durable service offset');
  // A server acknowledgement can be lost; an identical already-written range is safely acknowledged again.
  if(BigInt(current.offset)<state.offset)await client.call('transfers.downloadProgress',{transferId:transfer.transferId,offset:String(state.offset),complete:false});
  while(state.offset<state.total){
   if(disposed)throw failure('INTERRUPTED','Desktop closed');
   const length=Number(state.total-state.offset>BigInt(MAX_CHUNK)?BigInt(MAX_CHUNK):state.total-state.offset);
   const data=await client.download(entry,String(state.offset),length);if(String(data.revision)!==String(entry.revision)||data.bytes.length!==length)throw failure('SOURCE_CHANGED','Download range does not match its source revision');
   let written=0;while(written<data.bytes.length){const result=await state.output.write(data.bytes,written,data.bytes.length-written,state.offset+BigInt(written));if(!result.bytesWritten)throw failure('FULL_DISK','Download could not be written');written+=result.bytesWritten;}
   await state.output.sync();state.offset+=BigInt(length);
   await client.call('transfers.downloadProgress',{transferId:transfer.transferId,offset:String(state.offset),complete:false});
  }
  if(!state.published){
   // Re-stat the source after streaming before publishing the selected local result.
   await client.call('files.stat',{ref:entry});
   await state.output.sync();let name=state.original;
   for(let attempt=0;;attempt++){try{await fsp.link(beneath(state.target.directory,state.temp),beneath(state.target.directory,name));break;}catch(cause){if(cause.code!=='EEXIST')throw cause;if(request.conflictPolicy==='keep-both'&&attempt<1000){name=`${state.original.slice(0,220)} (${attempt+1})`;continue;}throw failure('CONFLICT','Download destination exists. Choose keep both or another folder.');}}
   await state.target.directory.sync();state.published=true;
  }
  await client.call('transfers.downloadProgress',{transferId:transfer.transferId,offset:String(state.total),complete:true});
  await state.output.close();await fsp.unlink(beneath(state.target.directory,state.temp));await state.target.close();record.partials.splice(record.partials.indexOf(state),1);transfer.nativeState=null;
 }
 async function collectDownloads(request){const items=[],roots=[];let visited=0;async function visit(entry,relative,depth){if(++visited>MAX_ITEMS||depth>64)throw failure('INVALID_REQUEST','Download selection exceeds the 1000-entry bound');if(entry.kind==='file'){items.push({entry:{id:entry.id,driveId:entry.driveId,path:entry.path,revision:entry.revision},relativePath:relative,kind:'file'});return;}if(entry.kind!=='directory')throw failure('UNSUPPORTED','Links and special files cannot be downloaded');let cursor=null,children=0;do{const page=await client.call('files.list',{query:{location:{driveId:entry.driveId,path:entry.path},requestId:`${request.requestId}:list:${visited}`,cursor,limit:500}});children+=page.entries.length;for(const child of page.entries)await visit(child,`${relative}/${child.name}`,depth+1);cursor=page.nextCursor;}while(cursor);if(!children)items.push({entry:{id:entry.id,driveId:entry.driveId,path:entry.path,revision:entry.revision},relativePath:relative,kind:'directory'});}
  for(const source of request.targets||[]){const entry=await client.call('files.stat',{ref:source});if(entry.kind==='directory')roots.push(entry.name);await visit(entry,entry.name,0);}return {items,roots};}
 async function streamDownloadDirectory(request,record,entry,relativePath,transfer){
  await client.call('files.stat',{ref:entry});const parts=relativePath.split('/'),original=parts.pop();if(!validName(original))throw failure('INVALID_PATH','Invalid download directory');const target=await destinationDirectory(record.destination,parts);let created;
  try{let name=original;for(let attempt=0;;attempt++){try{await fsp.mkdir(beneath(target.directory,name),{mode:0o700});created=name;break;}catch(cause){if(cause.code!=='EEXIST')throw cause;if(request.conflictPolicy==='keep-both'&&attempt<1000){name=`${original.slice(0,220)} (${attempt+1})`;continue;}throw failure('CONFLICT','Download directory exists. Choose keep both or another folder.');}}await target.directory.sync();await client.call('transfers.downloadProgress',{transferId:transfer.transferId,offset:'0',complete:true});}catch(cause){if(created)await fsp.rmdir(beneath(target.directory,created)).catch(()=>{});throw cause;}finally{await target.close();}
 }
 async function submitDownload(request,record){
  const {items,roots}=await collectDownloads(request);if(!items.length)throw failure('INVALID_REQUEST','Choose at least one regular file or directory');
  const sessions=items.length===1?[await client.call('transfers.download',{ref:items[0].entry,requestId:request.requestId,parentRequestId:request.requestId})]:(await client.call('transfers.batchDownload',{requestId:request.requestId,refs:items.map(item=>item.entry)})).sessions;
  if(!Array.isArray(sessions)||sessions.length!==items.length)throw failure('INVALID_RESPONSE','Service returned an invalid download reservation');
  const transfers=sessions.map((transfer,index)=>({...items[index],transfer}));const receipt={status:'accepted',requestId:request.requestId,jobIds:transfers.map(item=>item.transfer.jobId),simulated:false};
  let preparation;const prepare=()=>preparation??=(async()=>{for(const original of roots){if(!validName(original))throw failure('INVALID_PATH','Invalid download folder');let name=original;for(let attempt=0;;attempt++){try{await fsp.mkdir(beneath(record.destination,name),{mode:0o700});break;}catch(cause){if(cause.code!=='EEXIST')throw cause;if(request.conflictPolicy==='keep-both'&&attempt<1000){name=`${original.slice(0,220)} (${attempt+1})`;continue;}throw failure('CONFLICT','Download folder exists. Choose keep both or another destination.');}}for(const item of transfers)if(item.relativePath===original||item.relativePath.startsWith(original+'/')){item.relativePath=name+item.relativePath.slice(original.length);if(item.relativePath===name&&item.kind==='directory')item.preparedDirectory=true;}}await record.destination.sync();})();
  for(const item of transfers)resumable.set(item.transfer.jobId,{record,transferId:item.transfer.transferId,task:async()=>{await prepare();if(item.preparedDirectory){await client.call('files.stat',{ref:item.entry});return client.call('transfers.downloadProgress',{transferId:item.transfer.transferId,offset:'0',complete:true});}return item.kind==='directory'?streamDownloadDirectory(request,record,item.entry,item.relativePath,item.transfer):streamDownload(request,record,item.entry,item.relativePath,item.transfer);}});
  void run(request.requestId,record,async()=>{for(const item of transfers)await runTransfer(item.transfer.jobId);});return receipt;
 }
 function resumeJob(id,action){const item=resumable.get(id);if(!item||!['resume','retry'].includes(action))return;if(item.active){item.resumeRequested=true;return;}void run(id,item.record,()=>runTransfer(id));}
 async function refuseReselection(code,title,message){if(typeof dialog.showMessageBox==='function')await dialog.showMessageBox(getWindow(),{type:'error',title,message,buttons:['OK']});throw failure(code,message);}
 async function controlJob(id,action){
  if(disposed)throw failure('ABORTED','The native selection manager is closed');await expire();
  if(['resume','retry'].includes(action)&&await folderOwners?.isActive(id)){
   const job=await client.call('jobs.get',{id});
   if(['queued','waiting-for-client','running','verifying','committing'].includes(job.state))return job;
  }
  if(!['resume','retry'].includes(action)||resumable.has(id)){const result=await client.call('jobs.control',{id,action});resumeJob(id,action);return result;}
  let info;try{info=await client.call('transfers.resumeInfo',{jobId:id});}catch(cause){if(cause.detail?.code==='UNSUPPORTED')return client.call('jobs.control',{id,action});throw cause;}
  if(info?.direction==='folder'){
   const selection=await select('folder');if(!selection)throw failure('ABORTED','Reselection canceled');const record=handles.get(selection.handle);
   if(record.manifestDigest!==info.manifestDigest){handles.delete(selection.handle);await close(record);throw failure('SOURCE_CHANGED','Choose the unchanged original folder to resume this upload');}
   const result=await client.call('jobs.control',{id,action:'resume'});attachFolder({requestId:info.requestId,destination:info.destination},record,id);return result;
  }
  if(info?.jobId!==id||typeof info.transferId!=='string'||!info.transferId||info.transferId.length>256||!['upload','download'].includes(info.direction)||!validName(info.name)||typeof info.sizeBytes!=='string'||!/^(0|[1-9][0-9]{0,18})$/.test(info.sizeBytes)||typeof info.offset!=='string'||!/^(0|[1-9][0-9]{0,18})$/.test(info.offset)||BigInt(info.offset)>BigInt(info.sizeBytes))throw failure('INVALID_RESPONSE','The service returned invalid existing-transfer information');
  if(info.direction==='upload'&&(typeof info.sha256!=='string'||!(/^[a-f0-9]{64}$/).test(info.sha256)))return refuseReselection('UNSUPPORTED','Upload cannot be safely reselected','This existing upload has no recorded source SHA-256. Native resume is unavailable and the job is unchanged. Use the service transfer protocol to recover it or cancel the job.');
  let source;if(info.direction==='download'){validateAction('statFile',{ref:info.ref});source=await client.call('files.stat',{ref:info.ref});if(!['file','directory'].includes(source.kind)||String(source.sizeBytes??'0')!==info.sizeBytes)throw failure('SOURCE_CHANGED','The existing download source no longer matches its recorded revision and size');}
  const selection=await select(info.direction==='upload'?'files':'download-destination',info.direction==='upload'?{single:true,title:action==='retry'?'Reselect the original file to retry this upload':'Reselect the original file to resume this upload',buttonLabel:action==='retry'?'Verify and retry':'Verify and resume',message:'Only a file matching the original size and full SHA-256 can resume this existing job.'}:{title:'Restart this existing download from byte 0',buttonLabel:'Restart from byte 0',message:'The previous local partial is unavailable. Choose a destination to restart this same download job from byte 0. Existing files will not be overwritten.'});
  if(!selection)throw failure('ABORTED','Reselection canceled. The existing job and transfer were not changed.');
  const record=handles.get(selection.handle);record.busy=true;
  try{
   let task;
   if(info.direction==='upload'){
    const file=record.files[0];if(file.sizeBytes!==info.sizeBytes)return await refuseReselection('SOURCE_CHANGED','Upload source does not match','The selected file has a different size. The existing upload job and acknowledged bytes were not changed.');
    file.sha256=await hashSource(file);if(file.sha256!==info.sha256)return await refuseReselection('SOURCE_CHANGED','Upload source does not match','The selected file has a different SHA-256. The existing upload job and acknowledged bytes were not changed.');
    task=()=>streamUpload(file,info.transferId);
   }else{
    const transfer={transferId:info.transferId,jobId:id,sizeBytes:info.sizeBytes,revision:info.ref.revision};const request={requestId:`existing:${id}`,kind:'download',conflictPolicy:'ask'};
    task=()=>source.kind==='directory'?streamDownloadDirectory(request,record,info.ref,info.name,transfer):streamDownload(request,record,info.ref,info.name,transfer);
    await client.call('transfers.restartDownload',{transferId:info.transferId});
   }
   resumable.set(id,{record,transferId:info.transferId,task});const controlAction=info.direction==='download'?'resume':action;const result=await client.call('jobs.control',{id,action:controlAction});resumeJob(id,controlAction);return result;
  }catch(cause){handles.delete(selection.handle);await close(record);throw cause;}
 }
 async function runTransfer(jobId){const item=resumable.get(jobId);if(!item||item.active)return;item.active=true;try{await item.task();resumable.delete(jobId);}catch(cause){let state;try{state=(await client.call(item.folderId?'transfers.folderStatus':'transfers.query',item.folderId?{folderId:item.folderId}:{transferId:item.transferId})).state;}catch{/* Connection failure stays resumable. */}if(!disposed&&!['paused','pausing','canceled','canceling'].includes(state)){const allowed=['SOURCE_CHANGED','INTEGRITY_FAILED','FULL_DISK','CONFLICT','INTERRUPTED'];const code=allowed.includes(cause.detail?.code)?cause.detail.code:'INTERRUPTED';try{await client.call(item.folderId?'transfers.folderError':'transfers.clientError',item.folderId?{folderId:item.folderId,code}:{transferId:item.transferId,code});}catch{/* Service will report waiting for client if disconnected. */}}}finally{item.active=false;if(item.resumeRequested&&!disposed&&resumable.has(jobId)){item.resumeRequested=false;resumeJob(jobId,'resume');}}}
 async function run(requestId,record,task){record.activeCount=(record.activeCount||0)+1;record.busy=true;running.set(requestId,(running.get(requestId)||0)+1);try{await task();}catch(cause){/* Client disconnection is not cancellation: durable service jobs stay resumable. */receipts.set(`${requestId}:failure`,{code:cause.detail?.code||'INTERRUPTED'});}finally{if(running.get(requestId)===1)running.delete(requestId);else running.set(requestId,running.get(requestId)-1);record.activeCount--;record.busy=record.activeCount>0;record.expires=now()+TTL;}}
 return {select,resumeJob,controlJob,async submit(request){const known=receipts.get(request.requestId);if(known)return known;const record=selectionFor(request);record.busy=true;try{const result=await(request.kind==='upload'?submitUpload(request,record):submitDownload(request,record));receipts.set(request.requestId,result);if(receipts.size>128)receipts.delete(receipts.keys().next().value);return result;}catch(cause){record.busy=false;throw cause;}},reconcile:requestId=>receipts.get(requestId)||null,async dispose(){disposed=true;for(const record of handles.values())await close(record);handles.clear();resumable.clear();receipts.clear();},debugCounts(){return {handles:handles.size,opened:[...handles.values()].reduce((n,r)=>n+r.opened.length,0),files:[...handles.values()].reduce((total,item)=>total+item.files.length,0),running:running.size};}};
}
module.exports={createNativeSelections};
