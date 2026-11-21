import {useState} from 'react';
/** Presentation-only history. Files, jobs, receipts and retry identities remain authoritative. */
export function readQueueHidden(key?:string):string[]{
 if(!key)return [];
 try{const value:unknown=JSON.parse(localStorage.getItem(key)??'[]');return Array.isArray(value)?value.filter((v):v is string=>typeof v==='string'&&v.length<=256).slice(-10000):[];}catch{return [];}
}
export function saveQueueHidden(key:string|undefined,ids:string[]){
 if(key)try{localStorage.setItem(key,JSON.stringify([...new Set(ids)].slice(-10000)));}catch{/* Queue remains usable if optional presentation storage is unavailable. */}
}
export function useQueueHistory(key:string){
 const [hidden,setHidden]=useState(()=>readQueueHidden(key));
 const hide=(ids:string[])=>setHidden(old=>{const next=[...new Set([...old,...ids])].slice(-10000);saveQueueHidden(key,next);return next;});
 const restore=()=>{saveQueueHidden(key,[]);setHidden([]);};
 return {hidden:new Set(hidden),hide,restore};
}
