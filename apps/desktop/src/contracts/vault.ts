import {z} from 'zod';
export const vaultEntrySchema=z.object({id:z.string(),displayName:z.string(),url:z.string(),revision:z.number().int(),createdAt:z.number(),updatedAt:z.number(),trashed:z.boolean()}).strict();
export const vaultSecretSchema=vaultEntrySchema.extend({login:z.string(),password:z.string(),notes:z.string()});
export const vaultStatusSchema=z.object({enabled:z.boolean(),state:z.string(),writable:z.boolean(),replicasAvailable:z.number(),sequence:z.number()}).strict();
export const vaultListSchema=z.object({items:z.array(vaultEntrySchema),nextCursor:z.string().nullable()}).strict();
export const vaultHistorySchema=z.object({items:z.array(z.object({revision:z.number(),updatedAt:z.number(),trashed:z.boolean()}).strict()),nextCursor:z.string().nullable()}).strict();
export type VaultEntry=z.infer<typeof vaultEntrySchema>;
export type VaultSecret=z.infer<typeof vaultSecretSchema>;
export type VaultStatus=z.infer<typeof vaultStatusSchema>;
export type VaultFields={displayName:string;url:string;login:string;password:string;notes:string};
export interface VaultPort {
 issueKey(label:string):Promise<{id:string;secret:string}>;
 status():Promise<VaultStatus>;
 list(args:{query?:string;trashed?:boolean;cursor?:string;limit?:number}):Promise<z.infer<typeof vaultListSchema>>;
 reveal(id:string,version?:number):Promise<VaultSecret>;
 create(args:VaultFields&{requestId:string}):Promise<VaultEntry>;
 update(args:Partial<VaultFields>&{requestId:string;id:string;revision:number}):Promise<VaultEntry>;
 trash(args:{requestId:string;id:string;revision:number}):Promise<VaultEntry>;
 restore(args:{requestId:string;id:string;revision:number}):Promise<VaultEntry>;
 restoreVersion(args:{requestId:string;id:string;revision:number;version:number}):Promise<VaultEntry>;
 history(id:string,cursor?:string):Promise<z.infer<typeof vaultHistorySchema>>;
 generate(length:number):Promise<{password:string}>;
 copy(id:string,field:'login'|'password'):Promise<{copied:boolean}>;
}
