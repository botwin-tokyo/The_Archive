import {z} from 'zod';
import {locationSchema,entryRefSchema} from './schemas';
const id=z.string().min(1).max(256), text=z.string().max(500);
export const videoChoiceSchema=z.object({id,label:text,kind:z.enum(['video','audio']),height:z.number().int().nonnegative(),estimatedBytes:z.number().nonnegative().nullable()}).strict();
export const videoStatusSchema=z.object({enabled:z.boolean(),available:z.boolean(),defaultDestination:locationSchema.nullable(),supportedSites:z.array(text),maxBytes:z.string(),mcp:z.boolean()}).strict();
export const videoPlanSchema=z.object({id,revision:z.number().int().positive(),state:z.enum(['queued','running','completed','failed']),title:text,uploader:text,duration:z.number().nonnegative(),source:text,choices:z.array(videoChoiceSchema).max(20),expiresAt:z.number(),error:text.nullable()}).strict();
export const videoJobSchema=z.object({id,revision:z.number().int().positive(),state:z.enum(['queued','downloading','validating','importing','waiting-for-drive','paused','completed','failed','canceled']),title:text,destination:locationSchema,createdAt:z.number(),choice:videoChoiceSchema,progress:z.number().min(0).max(1).nullable(),error:text.nullable(),entry:entryRefSchema.nullable()}).strict();
export const videoJobsSchema=z.object({items:z.array(videoJobSchema).max(100),cursor:id.nullable()}).strict();
export type VideoPlan=z.infer<typeof videoPlanSchema>;
export type VideoJob=z.infer<typeof videoJobSchema>;
export interface VideoPort {
 status():Promise<z.infer<typeof videoStatusSchema>>;
 resolve(args:{requestId:string;url:string}):Promise<VideoPlan>;
 getPlan(id:string):Promise<VideoPlan>;
 enqueue(args:{requestId:string;planId:string;planRevision:number;choiceId:string;destination:z.infer<typeof locationSchema>}):Promise<VideoJob>;
 listJobs(args?:{cursor?:string;limit?:number}):Promise<z.infer<typeof videoJobsSchema>>;
 getJob(id:string):Promise<VideoJob>;
 control(args:{id:string;revision:number;action:'pause'|'resume'|'cancel'|'retry-failed'}):Promise<VideoJob>;
}
