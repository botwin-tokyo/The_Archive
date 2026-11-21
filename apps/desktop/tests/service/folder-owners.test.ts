// @vitest-environment node
import {createRequire} from 'node:module';
import {mkdtemp,rm,readFile,readdir,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {expect,it,vi} from 'vitest';
const require=createRequire(import.meta.url);
const {createFolderOwners}=require('../../electron/folder-owners.cjs');
const {createNativeSelections}=require('../../electron/native-selections.cjs');

it('a recreated desktop discovers a live folder uploader and does not open another picker',async()=>{
 const directory=await mkdtemp(path.join(tmpdir(),'archive-folder-owner-'));
 const config={endpoint:'http://127.0.0.1:18743',token:'test-owner-only'};
 const owner=createFolderOwners(config,{directory}),desktop=createFolderOwners(config,{directory});
 const job={id:'folder',operation:'upload',parentId:null,state:'waiting-for-client',capabilities:{resume:true,pause:true}};
 const dialog={showOpenDialog:vi.fn()},client={call:vi.fn(async()=>job)};
 const native=createNativeSelections({dialog,getWindow:()=>null,client,folderOwners:desktop});
 try{
  const release=await owner.register('folder');expect(await desktop.isActive('folder')).toBe(true);
  expect(await desktop.present(job)).toMatchObject({state:'running',capabilities:{resume:false,pause:true}});
  expect(await native.controlJob('folder','resume')).toEqual(job);
  expect(dialog.showOpenDialog).not.toHaveBeenCalled();expect(client.call.mock.calls).toEqual([['jobs.get',{id:'folder'}]]);
  expect(await desktop.present({...job,state:'paused'})).toMatchObject({state:'paused'});
  expect(await createFolderOwners({...config,token:'different-owner'},{directory}).isActive('folder')).toBe(false);
  await release();expect(await desktop.isActive('folder')).toBe(false);
 }finally{await native.dispose();await rm(directory,{recursive:true,force:true});}
});

it('rejects a reused PID and never stores source paths or credentials',async()=>{
 const directory=await mkdtemp(path.join(tmpdir(),'archive-folder-owner-'));const token='not-for-storage';
 const owners=createFolderOwners({endpoint:'http://127.0.0.1',token},{directory});
 try{
  await owners.register('folder');const file=path.join(directory,(await readdir(directory))[0]);const text=await readFile(file,'utf8');expect(text).not.toContain(token);
  await writeFile(file,JSON.stringify({...JSON.parse(text),processStart:'wrong-process-incarnation'}));
  expect(await owners.isActive('folder')).toBe(false);
  const release=await owners.register('folder');expect(await owners.isActive('folder')).toBe(true);await release();
 }finally{await rm(directory,{recursive:true,force:true});}
});
