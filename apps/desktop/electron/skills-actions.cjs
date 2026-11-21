'use strict';
const {z}=require('zod');
const id=z.string().min(1).max(256), revision=z.number().int().min(1).max(Number.MAX_SAFE_INTEGER);
const detail=z.object({id,revision:revision.optional()}).strict();
const file=z.object({id,path:z.string().min(1).max(1024),revision}).strict();
const change=z.object({id,expectedRevision:revision,requestId:id}).strict();
module.exports={
 skillsStatus:['skills.status',z.object({}).strict()],
 skillsList:['skills.list',z.object({query:z.string().max(256).optional(),cursor:z.string().max(256).optional(),limit:z.number().int().min(1).max(100).optional(),trashed:z.boolean().optional()}).strict()],
 skillsGet:['skills.get',detail],skillsDocument:['skills.document',detail],skillsFileRead:['skills.fileRead',file],
 skillsDownload:[null,file],skillsCopy:[null,z.object({id,revision}).strict()],
 skillsOpenLink:[null,z.object({url:z.string().min(1).max(4096)}).strict()],
 skillsTrash:['skills.trash',change],skillsRestore:['skills.restore',change],
 issueSkillsKey:[null,z.object({label:z.string().min(1).max(120)}).strict()]
};
