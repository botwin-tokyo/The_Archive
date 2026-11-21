'use strict';
const fs=require('node:fs/promises');
const {constants}=require('node:fs');
const {createHash,randomUUID}=require('node:crypto');
const {homedir}=require('node:os');
const path=require('node:path');

// Local discovery only. Backend offsets, authorization and writer leases remain
// authoritative. Never expose a local source path or credentials in these files.
function createFolderOwners(config,{directory=path.join(homedir(),'.config/archive-console/folder-owners')}={}){
 const scope=createHash('sha256').update(`${config.endpoint||''}\0${config.token||''}`).digest('hex');
 const filename=id=>path.join(directory,createHash('sha256').update(`${scope}\0${id}`).digest('hex')+'.json');
 async function identity(pid){
  try{const stat=await fs.readFile(`/proc/${pid}/stat`,'utf8'),fields=stat.slice(stat.lastIndexOf(')')+1).trim().split(/\s+/);return fields[0]==='Z'?null:fields[19];}catch{return null;}
 }
 async function record(id){
  let file;
  try{
   const dir=await fs.lstat(directory);if(!dir.isDirectory()||dir.uid!==process.getuid()||(dir.mode&0o077))return null;
   file=await fs.open(filename(id),constants.O_RDONLY|constants.O_NOFOLLOW);
   const stat=await file.stat();if(!stat.isFile()||stat.uid!==process.getuid()||(stat.mode&0o077)||stat.size>4096)return null;
   const value=JSON.parse(await file.readFile('utf8'));
   if(value.jobId!==id||!Number.isSafeInteger(value.pid)||value.pid<=0||typeof value.processStart!=='string'||typeof value.nonce!=='string')return null;
   return value;
  }catch{return null;}finally{await file?.close();}
 }
 async function isActive(id){const value=await record(id);return Boolean(value&&await identity(value.pid)===value.processStart);}
 return {
  isActive,
  async register(id,pid=process.pid){
   if(await isActive(id))return null;
   const processStart=await identity(pid);if(!processStart)throw new Error('Folder uploader process is unavailable');
   await fs.mkdir(directory,{recursive:true,mode:0o700});const dir=await fs.lstat(directory);
   if(!dir.isDirectory()||dir.uid!==process.getuid()||(dir.mode&0o077))throw new Error('Folder owner directory must be private and owned');
   const nonce=randomUUID(),value={jobId:id,pid,processStart,nonce},target=filename(id),temporary=target+'.'+nonce;
   await fs.writeFile(temporary,JSON.stringify(value),{flag:'wx',mode:0o600});await fs.rename(temporary,target);
   return async()=>{if((await record(id))?.nonce===nonce)await fs.unlink(target).catch(()=>{});};
  },
  async present(job){
   if(job?.operation==='upload'&&!job.parentId&&['queued','waiting-for-client'].includes(job.state)&&await isActive(job.id))return {...job,state:'running',capabilities:{...job.capabilities,resume:false,pause:true}};
   return job;
  },
 };
}
module.exports={createFolderOwners};
