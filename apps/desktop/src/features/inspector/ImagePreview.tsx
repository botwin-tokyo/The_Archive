import {useEffect,useState} from 'react';
import {Expand,ZoomIn,ZoomOut} from 'lucide-react';
import type {Entry,FileReadPort,ImagePreview as Preview} from '../../contracts';
import {refOf,safeError} from '../../contracts';
import {Dialog} from '../../shared/ui';

export function ImagePreview({entry,port}:{entry:Entry;port:FileReadPort}){
 const [preview,setPreview]=useState<Preview|null>(null),[error,setError]=useState(''),[attempt,setAttempt]=useState(0);
 const [open,setOpen]=useState(false),[zoom,setZoom]=useState<number|null>(null);
 const [natural,setNatural]=useState<{width:number;height:number}|null>(null);
 useEffect(()=>{
  const controller=new AbortController();let active=true;
  setPreview(null);setError('');setNatural(null);
  if(!port.previewImage){setError('Image previews are unavailable in this connection.');return;}
  port.previewImage(refOf(entry),{signal:controller.signal}).then(value=>{if(active)setPreview(value);}).catch(cause=>{if(active&&!controller.signal.aborted)setError(safeError(cause).message);});
  return()=>{active=false;controller.abort();};
 },[entry.id,entry.driveId,entry.path,entry.revision,port,attempt]);
 const failed=()=>{setPreview(null);setError('This image could not be decoded. It may be damaged or use an unsupported encoding.');};
 const message=error?<div role="alert" className="image-preview-error"><p>{error}</p><button className="btn" onClick={()=>setAttempt(n=>n+1)}>Retry image preview</button></div>:<p role="status">Loading image preview…</p>;
 const size=natural??preview;
 return <div className="image-preview" aria-label="Image preview">
  {preview?<><button className="image-preview-thumbnail" onClick={()=>{setZoom(null);setOpen(true);}} aria-label={`Open image preview: ${entry.name}`}><img src={preview.dataUrl} alt={entry.name} onError={failed} onLoad={event=>setNatural({width:event.currentTarget.naturalWidth,height:event.currentTarget.naturalHeight})}/><span><Expand size={16}/> Open preview</span></button><p className="image-preview-dimensions">{size?.width.toLocaleString()} × {size?.height.toLocaleString()} pixels</p></>:message}
  <Dialog open={open} title={entry.name} onClose={()=>setOpen(false)} className="image-preview-dialog">
   {preview?<><div className="image-preview-toolbar" role="group" aria-label="Image zoom"><button className="btn" onClick={()=>setZoom(null)} aria-pressed={zoom===null}>Fit image</button><button className="btn" onClick={()=>setZoom(1)} aria-pressed={zoom===1}>Actual size</button><button className="btn" aria-label="Zoom out" disabled={zoom!==null&&zoom<=.1} onClick={()=>setZoom(value=>Math.max(.1,(value??1)/1.5))}><ZoomOut size={18}/></button><span aria-live="polite">{zoom===null?'Fit':`${Math.round(zoom*100)}%`}</span><button className="btn" aria-label="Zoom in" disabled={zoom!==null&&zoom>=4} onClick={()=>setZoom(value=>Math.min(4,(value??1)*1.5))}><ZoomIn size={18}/></button></div><div className={`image-preview-canvas ${zoom===null?'image-preview-canvas--fit':''}`}><img src={preview.dataUrl} alt={entry.name} onError={failed} style={zoom===null?undefined:{width:(size?.width??preview.width)*zoom}}/></div></>:message}
  </Dialog>
 </div>;
}
