'use strict';
const {Buffer}=require('node:buffer');
const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');
module.exports=function createSkillsNative({client,dialog,getWindow,clipboard,shell}){
 return async function(action,input){
  if(action==='skillsCopy'){
   const value=await client.call('skills.document',input);
   if(typeof value.text!=='string'||Buffer.byteLength(value.text)>1048576)throw new Error('Invalid document');
   clipboard.writeText(value.text);return {copied:true};
  }
  if(action==='skillsOpenLink'){
   const url=new URL(input.url);
   if(!['https:','http:'].includes(url.protocol)||url.username||url.password||url.href.length>4096)throw new Error('Only HTTP(S) links are supported');
   await shell.openExternal(url.href);return {opened:true};
  }
  if(action==='issueSkillsKey'){
   const secret=crypto.randomBytes(48).toString('base64url');
   const created=await client.call('agents.register',{label:input.label,secret,scopes:['skills.read','skills.write'],allowedRoots:[]});
   return {id:created.id,secret};
  }
  const detail=await client.call('skills.get',{id:input.id,revision:input.revision});
  const file=detail.files.find(f=>f.path===input.path);
  if(!file||!Number.isSafeInteger(file.size)||file.size<0||file.size>16777216)throw new Error('Invalid file');
  const chosen=await dialog.showSaveDialog(getWindow(),{title:'Download skill file',defaultPath:path.basename(input.path)});
  if(chosen.canceled||!chosen.filePath)return {saved:false};
  const target=chosen.filePath,temporary=path.join(path.dirname(target),'.archive-skill-'+crypto.randomUUID()+'.partial');
  let handle;const hash=crypto.createHash('sha256');
  try{
   const downloaded=await client.downloadSkillFile(input);
   const bytes=downloaded.bytes;
   if(!Buffer.isBuffer(bytes)||bytes.length!==file.size||String(downloaded.revision)!==String(input.revision))throw new Error('Invalid download response');
   hash.update(bytes);
   handle=await fs.open(temporary,'wx',0o600);await handle.writeFile(bytes);
   if(hash.digest('hex')!==file.sha256)throw new Error('Download integrity check failed');
   await handle.sync();await handle.close();handle=null;
   await fs.rename(temporary,target);const directory=await fs.open(path.dirname(target),'r');try{await directory.sync();}finally{await directory.close();}
   return {saved:true};
  }finally{if(handle)await handle.close();await fs.rm(temporary,{force:true});}
 };
};
