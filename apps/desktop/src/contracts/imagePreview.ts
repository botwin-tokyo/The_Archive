import {z} from 'zod';
export const imagePreviewSchema=z.object({
 mime:z.enum(['image/png','image/jpeg','image/gif','image/webp']),
 width:z.number().int().positive().max(16384),height:z.number().int().positive().max(16384),
 sizeBytes:z.string().regex(/^[1-9][0-9]*$/).refine(value=>BigInt(value)<=16777216n),
 dataUrl:z.string().max(22369648).regex(/^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/]+={0,2}$/),
}).strict().refine(value=>value.width*value.height<=32000000&&value.dataUrl.startsWith(`data:${value.mime};base64,`));
export type ImagePreview= z.infer<typeof imagePreviewSchema>;
export function isPreviewImage(name:string):boolean{return /\.(?:png|jpe?g|gif|webp)$/i.test(name);}
