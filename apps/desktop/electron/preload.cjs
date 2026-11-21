'use strict';
const {contextBridge,ipcRenderer}=require('electron');
const bootstrap=ipcRenderer.sendSync('desktop:bootstrap');
if(!bootstrap||bootstrap.schemaVersion!==1||!['demo','service'].includes(bootstrap.mode)||typeof bootstrap.configured!=='boolean')throw new Error('Desktop configuration unavailable');
const invoke=(action,args)=>ipcRenderer.invoke(`archive:${action}`,args);
// Named calls only: no generic invoke, URL, host path, filesystem or token APIs.
const backend=Object.freeze({
issueVaultKey:args=>invoke('issueVaultKey',args),
skillsStatus:args=>invoke('skillsStatus',args),skillsList:args=>invoke('skillsList',args),skillsGet:args=>invoke('skillsGet',args),skillsDocument:args=>invoke('skillsDocument',args),skillsFileRead:args=>invoke('skillsFileRead',args),skillsDownload:args=>invoke('skillsDownload',args),skillsCopy:args=>invoke('skillsCopy',args),skillsOpenLink:args=>invoke('skillsOpenLink',args),skillsTrash:args=>invoke('skillsTrash',args),skillsRestore:args=>invoke('skillsRestore',args),issueSkillsKey:args=>invoke('issueSkillsKey',args),
vaultStatus:args=>invoke('vaultStatus',args),vaultHelp:args=>invoke('vaultHelp',args),vaultEntriesList:args=>invoke('vaultEntriesList',args),vaultEntriesGet:args=>invoke('vaultEntriesGet',args),vaultEntriesReveal:args=>invoke('vaultEntriesReveal',args),vaultEntriesCreate:args=>invoke('vaultEntriesCreate',args),vaultEntriesUpdate:args=>invoke('vaultEntriesUpdate',args),vaultEntriesTrash:args=>invoke('vaultEntriesTrash',args),vaultEntriesRestore:args=>invoke('vaultEntriesRestore',args),vaultEntriesHistory:args=>invoke('vaultEntriesHistory',args),vaultEntriesRestoreVersion:args=>invoke('vaultEntriesRestoreVersion',args),vaultPasswordsGenerate:args=>invoke('vaultPasswordsGenerate',args),vaultRequestsReconcile:args=>invoke('vaultRequestsReconcile',args),vaultBackupExport:args=>invoke('vaultBackupExport',args),vaultBackupImport:args=>invoke('vaultBackupImport',args),copyVaultSecret:args=>invoke('copyVaultSecret',args),
 retryGitBackup:args=>invoke('retryGitBackup',args),getGitBackupStatus:args=>invoke('getGitBackupStatus',args),configureGitBackup:args=>invoke('configureGitBackup',args),runGitBackup:args=>invoke('runGitBackup',args),reconcileGitBackup:args=>invoke('reconcileGitBackup',args),cancelGitBackup:args=>invoke('cancelGitBackup',args),previewGitBackup:args=>invoke('previewGitBackup',args),
 getAnimeStatus:args=>invoke('getAnimeStatus',args),
 searchAnime:args=>invoke('searchAnime',args),
 getAnimeSearch:args=>invoke('getAnimeSearch',args),
 lookupAnimeSeries:args=>invoke('lookupAnimeSeries',args),
 getAnimeSeries:args=>invoke('getAnimeSeries',args),
 getAnimeCover:args=>invoke('getAnimeCover',args),
 resolveAnime:args=>invoke('resolveAnime',args),
 getAnimePlan:args=>invoke('getAnimePlan',args),
 enqueueAnime:args=>invoke('enqueueAnime',args),
 reconcileAnime:args=>invoke('reconcileAnime',args),
 listAnimeJobs:args=>invoke('listAnimeJobs',args),
 getAnimeJob:args=>invoke('getAnimeJob',args),
 controlAnimeJob:args=>invoke('controlAnimeJob',args),
 getVideoStatus:args=>invoke('getVideoStatus',args),
 resolveVideo:args=>invoke('resolveVideo',args),
 getVideoPlan:args=>invoke('getVideoPlan',args),
 enqueueVideo:args=>invoke('enqueueVideo',args),
 listVideoJobs:args=>invoke('listVideoJobs',args),
 getVideoJob:args=>invoke('getVideoJob',args),
 controlVideoJob:args=>invoke('controlVideoJob',args),
 getMusicLyricsStatus:args=>invoke('getMusicLyricsStatus',args),
 backfillMusicLyrics:args=>invoke('backfillMusicLyrics',args),
 getMusicArtwork:args=>invoke('getMusicArtwork',args),
 getMusicStatus:args=>invoke('getMusicStatus',args),
 searchMusic:args=>invoke('searchMusic',args),
 getMusicSearch:args=>invoke('getMusicSearch',args),
 resolveMusic:args=>invoke('resolveMusic',args),
 getMusicPlan:args=>invoke('getMusicPlan',args),
 enqueueMusic:args=>invoke('enqueueMusic',args),
 listMusicJobs:args=>invoke('listMusicJobs',args),
 getMusicJob:args=>invoke('getMusicJob',args),
 controlMusicJob:args=>invoke('controlMusicJob',args),

 previewImage:args=>invoke('previewImage',args),
 listDrives:args=>invoke('listDrives',args),listFiles:args=>invoke('listFiles',args),statFile:args=>invoke('statFile',args),
 submitOperation:args=>invoke('submitOperation',args),reconcileOperation:args=>invoke('reconcileOperation',args),
 jobSnapshot:args=>invoke('jobSnapshot',args),jobEvents:args=>invoke('jobEvents',args),controlJob:args=>invoke('controlJob',args),
 listTrash:args=>invoke('listTrash',args),getConfiguration:args=>invoke('getConfiguration',args),saveConfiguration:args=>invoke('saveConfiguration',args),testHealth:args=>invoke('testHealth',args),
 listAgents:args=>invoke('listAgents',args),updateAgentScopes:args=>invoke('updateAgentScopes',args),revokeAgent:args=>invoke('revokeAgent',args),listDiagnostics:args=>invoke('listDiagnostics',args),getStatus:args=>invoke('getStatus',args),selectNative:args=>invoke('selectNative',args),
});
contextBridge.exposeInMainWorld('archiveDesktop',Object.freeze({mode:bootstrap.mode,backend,getVersion:async(...args)=>{if(args.length)throw new Error('Invalid bridge request');const result=await ipcRenderer.invoke('desktop:version');if(result?.schemaVersion!==1||!['demo','service'].includes(result?.mode)||typeof result.appVersion!=='string'||typeof result.configured!=='boolean')throw new Error('Invalid desktop response');return Object.freeze({schemaVersion:1,appVersion:result.appVersion,mode:result.mode,configured:result.configured});}}));
