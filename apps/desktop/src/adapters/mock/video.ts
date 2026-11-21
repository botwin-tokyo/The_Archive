import type {VideoPort,VideoPlan,VideoJob} from '../../contracts';
export function createMockVideo():VideoPort {
 const plans=new Map<string,VideoPlan>(),jobs=new Map<string,VideoJob>();
 return {status:async()=>({enabled:true,available:true,defaultDestination:null,supportedSites:['YouTube','X / Twitter'],maxBytes:'8589934592',mcp:true}),
 resolve:async args=>{const p:VideoPlan={id:args.requestId,revision:1,state:'completed',title:'Demo video preview',uploader:'Demo creator',duration:120,source:'youtube:demo',choices:[{id:'video-720',label:'720p · MP4',kind:'video',height:720,estimatedBytes:null},{id:'audio',label:'Audio only · MP3',kind:'audio',height:0,estimatedBytes:null}],expiresAt:Date.now()/1000+900,error:null};plans.set(p.id,p);return p;},
 getPlan:async id=>{const p=plans.get(id);if(!p)throw new Error('Preview not found');return p;},
 enqueue:async args=>{const p=plans.get(args.planId);const choice=p?.choices.find(c=>c.id===args.choiceId);if(!p||!choice)throw new Error('Preview not found');const j:VideoJob={id:args.requestId,revision:1,state:'queued',title:p.title,choice,destination:args.destination,createdAt:Date.now()/1000,progress:null,error:null,entry:null};jobs.set(j.id,j);return j;},
 listJobs:async()=>({items:[...jobs.values()],cursor:null}),getJob:async id=>{const j=jobs.get(id);if(!j)throw new Error('Job not found');return j;},
 control:async args=>{const j=jobs.get(args.id);if(!j)throw new Error('Job not found');const next={...j,revision:j.revision+1,state:args.action==='pause'?'paused' as const:args.action==='cancel'?'canceled' as const:'queued' as const};jobs.set(j.id,next);return next;}};
}
