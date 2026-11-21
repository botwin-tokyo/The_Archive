import {z} from 'zod';
import {locationSchema,entryRefSchema} from './schemas';
const text=z.string().max(500), id=z.string().min(1).max(256), count=z.number().int().nonnegative();
const audio=z.enum(['sub','dub']), year=z.number().int().min(1900).max(2200).nullable();
const formats=z.enum(['TV','TV_SHORT','MOVIE','SPECIAL','OVA','ONA','MUSIC']);
export const animeSubtitleLanguages=['en','es','pt','fr','de','it','ru','ar','ja','zh','ko','id','th','vi','pl','tr','hi','ms'] as const;
const subtitle=z.enum(animeSubtitleLanguages);
export const animeProviderSeriesIdSchema=z.string().regex(/^[a-z]{2,20}\/[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{1,12}$/);
export const animeEpisodeIdSchema=z.string().regex(/^[A-Za-z0-9._-]{1,200}$/);
export const animeEpisodeNumberSchema=z.string().regex(/^(0|[1-9][0-9]{0,4})(\.[0-9]{1,2})?$/);
export const animeQualitySchema=z.string().regex(/^(best|[1-9][0-9]{2,3}p)$/);
export const animeCatalogItemSchema=z.object({anilistId:z.number().int().positive(),malId:z.number().int().positive().nullable(),title:z.string().max(300),altTitles:z.array(z.string().max(300)).max(8),year,format:formats.nullable(),status:z.string().max(40).nullable(),episodes:count.nullable(),hasCover:z.boolean()}).strict();
export const animeStatusSchema=z.object({enabled:z.boolean(),available:z.boolean(),provider:text,catalog:text,message:text,defaultDestination:locationSchema.nullable(),maxEpisodesPerJob:z.number().int().positive(),audio:z.array(audio),mcp:z.boolean()}).strict();
export const animeSearchSchema=z.object({id,state:z.enum(['queued','running','completed','failed']),query:z.string().max(200),page:z.number().int().min(1).max(50),items:z.array(animeCatalogItemSchema).max(20),hasNextPage:z.boolean(),error:text.nullable(),cached:z.boolean().optional()}).strict();
export const animeCandidateSchema=z.object({providerSeriesId:animeProviderSeriesIdSchema,title:z.string().max(300),year,type:z.string().max(40).nullable(),episodeCount:count.nullable(),anilistId:z.number().int().positive().nullable(),malId:z.number().int().positive().nullable(),evidence:z.enum(['anilist-id','mal-id','title'])}).strict();
export const animeEpisodeSchema=z.object({id:animeEpisodeIdSchema,number:animeEpisodeNumberSchema,title:z.string().max(300).nullable(),sub:z.boolean(),dub:z.boolean()}).strict();
const sample=z.object({heights:z.array(z.number().int().min(144).max(4320)).min(1).max(8),subtitles:z.array(subtitle)}).strict().nullable();
export const animeSeriesSchema=z.object({id,revision:z.number().int().positive(),state:z.enum(['queued','running','completed','failed']),anilistId:z.number().int().positive(),requestedProvider:animeProviderSeriesIdSchema.nullable(),
 catalog:animeCatalogItemSchema.nullable(),description:z.string().max(6000).nullable(),genres:z.array(z.string().max(60)).max(20),
 relations:z.array(z.object({anilistId:z.number().int().positive(),title:z.string().max(300),relation:z.string().max(40),format:formats.nullable(),year}).strict()).max(40),
 candidates:z.array(animeCandidateSchema).max(20),match:z.object({state:z.enum(['pending','matched','selected','ambiguous','none']),providerSeriesId:animeProviderSeriesIdSchema.nullable(),evidence:z.enum(['anilist-id','mal-id','title']).nullable()}).strict(),
 episodes:z.array(animeEpisodeSchema).max(5000),samples:z.object({sub:sample,dub:sample}).strict(),expiresAt:z.number(),error:text.nullable()}).strict();
const planStatus=z.enum(['new','already-present','conflict','unavailable']);
export const animePlanSchema=z.object({id,revision:z.number().int().positive(),state:z.literal('ready'),seriesId:id,anilistId:z.number().int().positive(),title:z.string().max(300),providerSeriesId:animeProviderSeriesIdSchema,audio,quality:animeQualitySchema,subtitles:z.array(subtitle),
 destination:locationSchema,folder:z.string().max(4096),episodes:z.array(z.object({id:animeEpisodeIdSchema,number:animeEpisodeNumberSchema,title:z.string().max(300).nullable(),name:z.string().max(255),path:z.string().max(4096),status:planStatus,reason:text.nullable(),entry:entryRefSchema.nullable()}).strict()).max(200),
 counts:z.object({total:count,new:count,'already-present':count,conflict:count,unavailable:count}).strict(),estimate:text,expiresAt:z.number()}).strict();
export const animeEpisodeStates=['queued','resolving','downloading','verifying','ready','importing','completed','already-present','paused','waiting-for-drive','blocked-auth','conflict','unavailable','failed','canceled'] as const;
export const animeJobEpisodeSchema=z.object({id:animeEpisodeIdSchema,number:animeEpisodeNumberSchema,title:z.string().max(300).nullable(),name:z.string().max(255),state:z.enum(animeEpisodeStates),stage:z.enum(['resolving','downloading','verifying']).nullable(),
 progress:z.number().min(0).max(1).nullable(),attempt:count,error:text.nullable(),entry:entryRefSchema.nullable(),height:z.number().int().positive().nullable(),sizeBytes:z.string().regex(/^[0-9]{1,20}$/).nullable()}).strict();
export const animeJobSchema=z.object({id,revision:z.number().int().positive(),state:z.enum(['queued','running','paused','waiting-for-drive','completed','partial','failed','canceled']),title:z.string().max(300),anilistId:z.number().int().positive(),providerSeriesId:animeProviderSeriesIdSchema,
 audio,quality:animeQualitySchema,subtitles:z.array(subtitle),destination:locationSchema,folder:z.string().max(4096),createdAt:z.number(),episodes:z.array(animeJobEpisodeSchema).max(200),
 summary:z.object({total:count,completed:count,failed:count,unavailable:count,canceled:count,pending:count}).strict().nullable()}).strict();
export const animeJobsSchema=z.object({items:z.array(animeJobSchema).max(100),cursor:id.nullable()}).strict();
export const animeReconcileSchema=z.object({action:z.enum(['search','lookup','resolve','enqueue']),id,state:z.string().max(40)}).strict().nullable();
export const animeCoverSchema=z.object({dataUrl:z.string().max(700000).regex(/^data:image\/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/).nullable()}).strict();
export type AnimeStatus=z.infer<typeof animeStatusSchema>;
export type AnimeCatalogItem=z.infer<typeof animeCatalogItemSchema>;
export type AnimeSearch=z.infer<typeof animeSearchSchema>;
export type AnimeSeries=z.infer<typeof animeSeriesSchema>;
export type AnimePlan=z.infer<typeof animePlanSchema>;
export type AnimeJob=z.infer<typeof animeJobSchema>;
export type AnimeJobEpisode=z.infer<typeof animeJobEpisodeSchema>;
export type AnimeAudio=z.infer<typeof audio>;
export type AnimeSubtitleLanguage=z.infer<typeof subtitle>;
export type AnimeJobAction='pause'|'resume'|'cancel'|'retry-failed';
export interface AnimePort {
 status():Promise<AnimeStatus>;
 search(args:{requestId:string;query:string;page?:number}):Promise<AnimeSearch>;
 getSearch(id:string):Promise<AnimeSearch>;
 lookup(args:{requestId:string;anilistId:number;providerSeriesId?:string}):Promise<AnimeSeries>;
 getSeries(id:string):Promise<AnimeSeries>;
 cover(anilistId:number):Promise<{dataUrl:string|null}>;
 resolve(args:{requestId:string;seriesId:string;seriesRevision:number;episodeIds:string[];audio:AnimeAudio;quality:string;subtitles:AnimeSubtitleLanguage[];destination:z.infer<typeof locationSchema>}):Promise<AnimePlan>;
 getPlan(id:string):Promise<AnimePlan>;
 enqueue(args:{requestId:string;planId:string;planRevision:number}):Promise<AnimeJob>;
 reconcile(requestId:string):Promise<z.infer<typeof animeReconcileSchema>>;
 listJobs(args?:{cursor?:string;limit?:number}):Promise<z.infer<typeof animeJobsSchema>>;
 getJob(id:string):Promise<AnimeJob>;
 control(args:{id:string;revision:number;action:AnimeJobAction}):Promise<AnimeJob>;
}
