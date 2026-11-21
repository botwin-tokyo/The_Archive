// Local generation only. Reuses the deployed Python input and desktop DTO definitions.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
import ts from 'typescript';
import {z} from 'zod';
const root=process.cwd();
const runtime=path.join(root,'.runtime');
fs.mkdirSync(runtime,{recursive:true});
const scratch=fs.mkdtempSync(path.join(runtime,'mobile-contracts-'));
try {
  fs.writeFileSync(path.join(scratch,'package.json'),'{"type":"commonjs"}');
  for(const name of ['schemas','music','video']) {
    const source=fs.readFileSync(`apps/desktop/src/contracts/${name}.ts`,'utf8');
    fs.writeFileSync(path.join(scratch,`${name}.js`),ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText);
  }
  const require=createRequire(import.meta.url);
  const music=require(path.join(scratch,'music.js')),video=require(path.join(scratch,'video.js')),common=require(path.join(scratch,'schemas.js'));
  const methods=JSON.parse(execFileSync('python3',['-c','import json; from archive_backend.mobile_acquisitions import METHODS,input_schema; print(json.dumps({m:{"request":input_schema(m)} for m in METHODS}))'],{env:{...process.env,PYTHONPATH:path.join(root,'apps/backend/src')},encoding:'utf8'}));
  const responses={
    'video.status':video.videoStatusSchema,'video.resolve':video.videoPlanSchema,'video.plan.get':video.videoPlanSchema,
    'video.enqueue':video.videoJobSchema,'video.jobs.get':video.videoJobSchema,'video.jobs.control':video.videoJobSchema,'video.jobs.list':video.videoJobsSchema,
    'music.status':music.musicStatusSchema,'music.search':music.musicSearchSchema,'music.search.get':music.musicSearchSchema,
    'music.resolve':music.musicPlanSchema,'music.plan.get':music.musicPlanSchema,'music.artwork.get':z.object({dataUrl:z.string().nullable()}).strict(),
    'music.enqueue':music.musicJobSchema,'music.jobs.get':music.musicJobSchema,'music.jobs.control':music.musicJobSchema,'music.jobs.list':music.musicJobsSchema,
  };
  for(const [name,schema] of Object.entries(responses)) methods[name].response=z.toJSONSchema(schema);
  const submissions=['music.search','music.resolve','music.enqueue','video.resolve','video.enqueue'];
  methods['acquisitions.reconcile']={request:z.toJSONSchema(z.object({method:z.enum(submissions),requestId:z.string().min(1).max(256)}).strict()),response:{oneOf:submissions.map(method=>({type:'object',additionalProperties:false,required:['method','requestId','result'],properties:{method:{const:method},requestId:{type:'string',minLength:1,maxLength:256},result:methods[method].response}}))}};
  const profile=JSON.parse(fs.readFileSync('contracts/mobile/mobile-api.schema.json','utf8'));
  const storage={type:'object',additionalProperties:false,required:['schemaVersion','maxRoots','drives','roots'],properties:{schemaVersion:{const:1},maxRoots:{const:128},drives:{type:'array',maxItems:128,items:z.toJSONSchema(common.driveSchema)},roots:profile.$defs.Profile.properties.roots},$defs:profile.$defs};
  const discovery=z.toJSONSchema(z.object({schemaVersion:z.literal(1),protocol:z.literal('archive_mobile_acquisition_v1'),methods:z.array(z.string()),integrations:z.array(z.object({id:z.enum(['music','video']),label:z.string(),configured:z.boolean(),available:z.boolean(),allowed:z.boolean(),reason:z.string().nullable()}).strict()),maxRoots:z.literal(128),pollSeconds:z.literal(2),push:z.literal(false),jobOwnership:z.literal('account'),continuesAfterLogout:z.literal(true),searchKinds:z.array(z.enum(['track','album','artist'])),playlistSupported:z.literal(false),supportedSites:z.array(z.string())}).strict());
  const result={discovery,protocol:'archive_mobile_acquisition_v1',schemaVersion:1,http:{discovery:'GET /mobile/v1/acquisitions',call:'POST /mobile/v1/acquisitions/call',storage:'GET /mobile/v1/storage',auth:'mobile access bearer'},methods,storage};
  fs.writeFileSync('contracts/mobile/acquisition-api.schema.json',JSON.stringify(result,null,2)+'\n');
} finally {fs.rmSync(scratch,{recursive:true,force:true});}
