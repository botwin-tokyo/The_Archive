import {z} from 'zod';
export const skillEntrySchema=z.object({id:z.string(),slug:z.string(),name:z.string(),description:z.string(),revision:z.number().int(),createdAt:z.number(),updatedAt:z.number(),trashed:z.boolean(),warnings:z.array(z.string())}).strict();
export const skillFileSchema=z.object({path:z.string(),size:z.number().int(),sha256:z.string()}).strict();
export const skillDetailSchema=skillEntrySchema.extend({contentRevision:z.number().int(),files:z.array(skillFileSchema),revisions:z.array(z.number().int())});
export const skillStatusSchema=z.object({enabled:z.boolean(),state:z.string(),usageBytes:z.number(),limits:z.record(z.string(),z.number())}).strict();
export const skillListSchema=z.object({items:z.array(skillEntrySchema),nextCursor:z.string().nullable()}).strict();
export const skillDocumentSchema=z.object({text:z.string(),revision:z.number().int()}).strict();
export const skillReadSchema=z.object({encoding:z.literal('base64'),data:z.string(),size:z.number(),revision:z.number().int(),nextOffset:z.number().nullable()}).strict();
export type SkillEntry=z.infer<typeof skillEntrySchema>;
export type SkillDetail=z.infer<typeof skillDetailSchema>;
export interface SkillsPort {
 status():Promise<z.infer<typeof skillStatusSchema>>;
 list(args:{query?:string;cursor?:string;limit?:number;trashed?:boolean}):Promise<z.infer<typeof skillListSchema>>;
 get(id:string,revision?:number):Promise<SkillDetail>;
 document(id:string,revision?:number):Promise<z.infer<typeof skillDocumentSchema>>;
 fileRead(id:string,path:string,revision:number):Promise<z.infer<typeof skillReadSchema>>;
 download(id:string,path:string,revision:number):Promise<{saved:boolean}>;
 copy(id:string,revision:number):Promise<{copied:boolean}>;
 openLink(url:string):Promise<{opened:boolean}>;
 trash(args:{requestId:string;id:string;expectedRevision:number}):Promise<SkillEntry>;
 restore(args:{requestId:string;id:string;expectedRevision:number}):Promise<SkillEntry>;
 issueKey(label:string):Promise<{id:string;secret:string}>;
}
