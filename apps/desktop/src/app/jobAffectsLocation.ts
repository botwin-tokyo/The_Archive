import type {Job,Location} from '../contracts';

/** Public job destinations are folders; sources are the affected entries. */
export function jobAffectsLocation(job:Job, location:Location):boolean {
 if(job.operation==='download')return false;
 const parent=(path:string)=>path.split('/').slice(0,-1).join('/');
 const destination=job.destination;
 if(destination?.driveId===location.driveId && (destination.path===location.path || parent(destination.path)===location.path))return true;
 const source=job.source;
 if(source?.driveId===location.driveId && !['copy','upload','new-folder'].includes(job.operation) &&
   (parent(source.path)===location.path || source.path===location.path || location.path.startsWith(source.path+'/')))return true;
 return !source&&!destination;
}
