import {AlertTriangle,CheckCircle2,FileVideo,MinusCircle} from 'lucide-react';
import type {AnimePlan} from '../../contracts';
import {archivePathLabel} from '../../shared/lib/archiveLabels';
import {Dialog} from '../../shared/ui';
import {LANGUAGE} from './AnimeDetails';

const STATUS={new:{label:'Will download',icon:FileVideo},'already-present':{label:'Already in library',icon:CheckCircle2},conflict:{label:'Name conflict',icon:AlertTriangle},unavailable:{label:'Unavailable',icon:MinusCircle}} as const;

export function AnimeReviewDialog({plan,driveLabel,busy,error,onConfirm,onClose}:{plan:AnimePlan;driveLabel:string;busy:boolean;error:string;onConfirm:()=>void;onClose:()=>void}) {
 const {counts}=plan;
 return <Dialog open className="anime-review" title={`Review ${plan.title}`} onClose={onClose}
  description="Nothing is downloaded until you queue. Each episode is verified before it is saved; existing files are never overwritten."
  footer={<><button className="btn" onClick={onClose}>Back</button><button className="btn primary" disabled={busy||counts.new===0} onClick={onConfirm}>{counts.new?`Queue ${counts.new} episode${counts.new===1?'':'s'}`:'Nothing new to queue'}</button></>}>
  <dl className="anime-review-facts">
   <div><dt>Source</dt><dd>ani.pm · {plan.providerSeriesId}</dd></div>
   <div><dt>Audio</dt><dd>{plan.audio==='sub'?'Japanese (sub)':'English (dub)'}</dd></div>
   <div><dt>Quality</dt><dd>{plan.quality==='best'?'Best available per episode':plan.quality}</dd></div>
   <div><dt>Subtitles</dt><dd>{plan.subtitles.length?plan.subtitles.map(s=>LANGUAGE[s]??s).join(', '):'None'}</dd></div>
   <div><dt>Folder</dt><dd>{driveLabel} / {archivePathLabel(plan.folder)}</dd></div>
   <div><dt>Size</dt><dd>{plan.estimate}</dd></div>
  </dl>
  <p className="anime-review-counts" role="status">{counts.new} to download · {counts['already-present']} already present · {counts.conflict} conflict{counts.conflict===1?'':'s'} · {counts.unavailable} unavailable</p>
  {counts.conflict>0&&<p className="notice notice--warning"><AlertTriangle size={15}/> Conflicting names are skipped. Move or rename the existing file, then retry those episodes.</p>}
  <ol className="anime-review-list">
   {plan.episodes.map(e=>{const s=STATUS[e.status];const Icon=s.icon;return <li key={e.id} className={`is-${e.status}`}><Icon size={15}/><span>{e.name}</span><small>{s.label}{e.reason?` — ${e.reason}`:''}</small></li>;})}
  </ol>
  {error&&<p role="alert" className="anime-inline-error">{error}</p>}
 </Dialog>;
}
