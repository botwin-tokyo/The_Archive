import {useEffect,useState} from 'react';
import type {BackendPorts} from '../../contracts';

// Covers arrive as validated raster data URLs from the service; the renderer never fetches remote images.
const cache=new Map<string,string|null>();

export function useAnimeCover(ports:BackendPorts,anilistId:number|null,available:boolean){
 const key=`${ports.mode}:${anilistId}`;
 const [cover,setCover]=useState<string|null>(()=>cache.get(key)??null);
 useEffect(()=>{
  if(!anilistId||!available){setCover(null);return;}
  if(cache.has(key)){setCover(cache.get(key)??null);return;}
  let active=true;
  ports.anime.cover(anilistId).then(r=>{cache.set(key,r.dataUrl);if(active)setCover(r.dataUrl);}).catch(()=>{if(active)setCover(null);});
  return()=>{active=false;};
 },[ports,anilistId,available,key]);
 return cover;
}
