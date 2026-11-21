import {locationSchema,type Location} from '../contracts';
import {INITIAL_LOCATION,FILES_DRIVE_ID,BACKUPS_DRIVE_ID} from '../fixtures/drives';
const key='archive-console:last-location:v1';
export function readLastLocation(mode:'mock'|'service'='mock'):Location{try{const fallback=mode==='mock'?INITIAL_LOCATION:{driveId:'unconfigured',path:''};const raw=localStorage.getItem(`${key}:${mode}`)??(mode==='mock'?localStorage.getItem(key):null);if(!raw)return fallback;const result=locationSchema.safeParse(JSON.parse(raw));return result.success&&(mode==='service'||[FILES_DRIVE_ID,BACKUPS_DRIVE_ID].includes(result.data.driveId))?result.data:fallback;}catch{return mode==='mock'?INITIAL_LOCATION:{driveId:'unconfigured',path:''};}}
export function saveLastLocation(location:Location,mode:'mock'|'service'='mock'){try{localStorage.setItem(mode==='mock'?key:`${key}:${mode}`,JSON.stringify(locationSchema.parse(location)));}catch{/* Unavailable preference storage must not prevent browsing. */}}
