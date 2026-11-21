'use strict';
const {app,BrowserWindow,ipcMain,session,dialog}=require('electron');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {ACTIONS,readServiceConfig,createServiceClient,validateAction,failure,safeFailure,submitPurge}=require('./service-client.cjs');
const {createNativeSelections}=require('./native-selections.cjs');
const {createFolderOwners}=require('./folder-owners.cjs');
let window,recovered=false,pickerBusy=false,config,configurationError;
try{config=readServiceConfig();}catch(cause){config={mode:'service',configured:false};configurationError=safeFailure(cause);}
const client=createServiceClient(config);
const folderOwners=createFolderOwners(config);
const previewImage=require('./image-preview.cjs').createImagePreview(client);
let copyVaultSecret,skillsNative;
const native=createNativeSelections({dialog,getWindow:()=>window,client,folderOwners});
const entry=path.join(__dirname,'../dist/index.html');
const entryUrl=pathToFileURL(entry).href;
function trusted(event){
 if(!window||window.isDestroyed()||event.sender!==window.webContents||event.senderFrame!==window.webContents.mainFrame)throw failure('PERMISSION_DENIED','Untrusted bridge sender');
 let url;try{url=new URL(event.senderFrame.url);}catch{throw failure('PERMISSION_DENIED','Untrusted bridge origin');}
 url.hash='';url.search='';if(url.href!==entryUrl)throw failure('PERMISSION_DENIED','Untrusted bridge origin');
}
const version=()=>({schemaVersion:1,appVersion:app.getVersion(),mode:config.mode,configured:Boolean(config.configured)});
ipcMain.on('desktop:bootstrap',(event,...args)=>{try{trusted(event);if(args.length)throw failure('INVALID_REQUEST','Unexpected arguments');event.returnValue=version();}catch{event.returnValue=null;}});
ipcMain.handle('desktop:version',(event,...args)=>{trusted(event);if(args.length)throw failure('INVALID_REQUEST','Unexpected arguments');return version();});
for(const action of Object.keys(ACTIONS))ipcMain.handle(`archive:${action}`,async(event,...args)=>{
 try{
  trusted(event);if(config.mode!=='service'||args.length!==1)throw failure('INVALID_REQUEST','Invalid service bridge request');
  const input=validateAction(action,args[0]);
  if(configurationError)throw failure(configurationError.code,'Desktop service configuration is invalid');
  let result;
  if(action==='previewImage')result=await previewImage(input.ref);
  else if(action==='selectNative'){
   if(!client.configured)throw failure('UNAVAILABLE','Configure the service before selecting files');
   if(pickerBusy)throw failure('CONFLICT','A native picker is already open');pickerBusy=true;try{result=await native.select(input.kind);}finally{pickerBusy=false;}
  }else if(action==='submitOperation'&&['upload','download'].includes(input.request.kind))result=await native.submit(input.request);
  else if(action==='submitOperation'&&input.request.kind==='purge')result=await submitPurge(client,input.request);
  else if(action==='reconcileOperation')result=native.reconcile(input.requestId)||await client.call(ACTIONS[action][0],input);
  else if(action==='controlJob'){if(pickerBusy)throw failure('CONFLICT','A native picker is already open');pickerBusy=true;try{result=await native.controlJob(input.id,input.action);}finally{pickerBusy=false;}}
  else if(['skillsDownload','skillsCopy','skillsOpenLink','issueSkillsKey'].includes(action)){if(pickerBusy)throw failure('CONFLICT','A native action is already open');pickerBusy=true;try{if(!skillsNative)skillsNative=require('./skills-native.cjs')({client,dialog,getWindow:()=>window,clipboard:require('electron').clipboard,shell:require('electron').shell});result=await skillsNative(action,input);}finally{pickerBusy=false;}}
  else if(action==='issueVaultKey'){const secret=require('node:crypto').randomBytes(48).toString('base64url');const created=await client.call('agents.register',{label:input.label,secret,scopes:['vault.read','vault.reveal','vault.write','vault.export'],allowedRoots:[]});result={id:created.id,secret};}
  else if(action==='copyVaultSecret'){const value=await client.call('vault.entries.reveal',{id:input.id});const text=value[input.field];if(typeof text!=='string')throw failure('INVALID_RESPONSE','Invalid vault response');if(!copyVaultSecret)copyVaultSecret=require('./vault-clipboard.cjs')(require('electron').clipboard);copyVaultSecret(text);result={copied:true};}
  else result=await client.call(ACTIONS[action][0],input);
  if(action==='jobSnapshot')result={...result,jobs:await Promise.all(result.jobs.map(job=>folderOwners.present(job)))};
  if(action==='jobEvents')result={...result,events:await Promise.all(result.events.map(async event=>({...event,job:await folderOwners.present(event.job)})))};
  if(action==='controlJob')result=await folderOwners.present(result);
  return {version:1,mode:'service',ok:true,result};
 }catch(cause){return {version:1,mode:'service',ok:false,error:safeFailure(cause)};}
});
function createWindow(){
 window=new BrowserWindow({width:1600,height:1000,minWidth:900,minHeight:600,backgroundColor:'#0c100f',title:'The Archive',webPreferences:{preload:path.join(__dirname,'preload.cjs'),sandbox:true,contextIsolation:true,nodeIntegration:false,webSecurity:true}});
 window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
 window.webContents.on('will-navigate',event=>event.preventDefault());
 window.webContents.on('will-attach-webview',event=>event.preventDefault());
 window.webContents.on('render-process-gone',()=>{if(window.isDestroyed())return;if(!recovered){recovered=true;window.loadFile(entry,{hash:'renderer-recovered'});}else dialog.showMessageBox(window,{type:'error',title:'Renderer stopped',message:config.mode==='demo'?'The demo renderer stopped. Synthetic data resets when reopened.':'The renderer stopped. Reopen The Archive and check the durable service queue. The service is independent.'});});
 window.loadFile(entry);
}
app.whenReady().then(()=>{session.defaultSession.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));session.defaultSession.setPermissionCheckHandler(()=>false);createWindow();app.on('activate',()=>{if(BrowserWindow.getAllWindows().length===0)createWindow();});});
app.on('window-all-closed',()=>{void native.dispose();app.quit();});
