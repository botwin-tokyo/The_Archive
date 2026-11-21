'use strict';
// Operator recovery uses the same source-identity, range and checksum checks as
// Electron. Stop the previous uploader before handing its job to this process.
const path=require('node:path');
const {createServiceClient,readServiceConfig}=require('../apps/desktop/electron/service-client.cjs');
const {createNativeSelections}=require('../apps/desktop/electron/native-selections.cjs');
const {createFolderOwners}=require('../apps/desktop/electron/folder-owners.cjs');

async function main(){
 const [jobId,source,...extra]=process.argv.slice(2);
 if(!jobId||!source||extra.length)throw new Error('Usage: node scripts/resume-folder-upload.cjs JOB_ID ORIGINAL_FOLDER (service environment required)');
 const config=readServiceConfig(),transport=createServiceClient(config),folderOwners=createFolderOwners(config);
 const report=async(operation,task)=>{try{return await task();}catch(cause){console.error(JSON.stringify({operation,code:cause.detail?.code,message:cause.detail?.message||cause.message}));throw cause;}};
 const client={...transport,call:(capability,args)=>report(capability,()=>transport.call(capability,args)),upload:(id,offset,bytes)=>report('upload',()=>transport.upload(id,offset,bytes))};
 const info=await client.call('transfers.resumeInfo',{jobId});
 if(info.direction!=='folder')throw new Error('The existing job is not a folder upload');
 if(info.state==='completed'){console.log(JSON.stringify({jobId,state:'completed'}));return;}
 const native=createNativeSelections({client,folderOwners,getWindow:()=>null,dialog:{showOpenDialog:async()=>({canceled:false,filePaths:[path.resolve(source)]})}});
 try{
  await native.controlJob(jobId,'resume');
  do{
   await new Promise(resolve=>setTimeout(resolve,10000));
   try{
    const job=await client.call('jobs.get',{id:jobId});
    console.log(JSON.stringify({jobId,state:job.state,bytesDone:job.bytesDone,bytesTotal:job.bytesTotal,error:job.error}));
   }catch(cause){if(!cause.detail?.retryable)throw cause;}
  }while(native.debugCounts().running);
  const status=await client.call('transfers.folderStatus',{folderId:jobId});
  if(status.state!=='completed')throw new Error(`Folder stopped: ${status.error?.message||status.state}`);
  console.log(JSON.stringify({jobId,state:'completed',verifiedFiles:status.completedFiles,verifiedBytes:status.completedBytes}));
 }finally{await native.dispose();}
}
main().catch(cause=>{console.error(cause.detail?.message||cause.message);process.exitCode=1;});
