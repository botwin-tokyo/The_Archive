import {Tv} from 'lucide-react';
import type {AnimeCatalogItem,BackendPorts} from '../../contracts';
import {useAnimeCover} from './useAnimeCover';

const FORMAT:Record<string,string>={TV:'TV',TV_SHORT:'TV short',MOVIE:'Movie',SPECIAL:'Special',OVA:'OVA',ONA:'ONA',MUSIC:'Music'};

export function formatLine(item:Pick<AnimeCatalogItem,'year'|'format'|'episodes'|'status'>){
 return [item.year??'Year unknown',item.format?FORMAT[item.format]:null,item.episodes===null?'Episode count unknown':`${item.episodes} ep${item.episodes===1?'':'s'} (catalog)`].filter(Boolean).join(' · ');
}

export function AnimeCover({ports,item,size='card'}:{ports:BackendPorts;item:Pick<AnimeCatalogItem,'anilistId'|'title'|'hasCover'>;size?:'card'|'hero'}) {
 const cover=useAnimeCover(ports,item.anilistId,item.hasCover);
 return <div className={`anime-cover anime-cover--${size}`} aria-hidden="true">{cover?<img src={cover} alt=""/>:<Tv size={size==='hero'?44:30} strokeWidth={1.4}/>}</div>;
}

export function AnimeResults({ports,items,onOpen}:{ports:BackendPorts;items:AnimeCatalogItem[];onOpen:(item:AnimeCatalogItem)=>void}) {
 return <ul className="anime-results" aria-label="Search results">
  {items.map(item=><li key={item.anilistId}>
   <button className="anime-result" onClick={()=>onOpen(item)} aria-label={`${item.title}, ${formatLine(item)}`}>
    <AnimeCover ports={ports} item={item}/>
    <strong>{item.title}</strong>
    {item.altTitles[0]&&<span>{item.altTitles[0]}</span>}
    <small>{formatLine(item)}</small>
   </button>
  </li>)}
 </ul>;
}
