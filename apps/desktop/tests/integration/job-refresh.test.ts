import {expect,it} from 'vitest';
import {jobAffectsLocation} from '../../src/app/jobAffectsLocation';
import {createJobs} from '../../src/fixtures/jobs';
import type {Job} from '../../src/contracts';
const upload:Job={...createJobs()[0],operation:'upload',source:null,destination:{driveId:'files',path:'Music/Artist/Album/Disc 01'}};
it('does not refresh unrelated folders for music imports',()=>{
 expect(jobAffectsLocation(upload,{driveId:'files',path:'Documents'})).toBe(false);
 expect(jobAffectsLocation(upload,{driveId:'files',path:'Music'})).toBe(false);
 expect(jobAffectsLocation(upload,{driveId:'backups',path:''})).toBe(false);
 expect(jobAffectsLocation(upload,{driveId:'files',path:'Music/Artist/Album/Disc 01'})).toBe(true);
 expect(jobAffectsLocation(upload,{driveId:'files',path:'Music/Artist/Album'})).toBe(true);
});
it('updates newly created folders and moved source locations',()=>{
 expect(jobAffectsLocation({...upload,operation:'new-folder',destination:{driveId:'files',path:'Music'}},{driveId:'files',path:'Music'})).toBe(true);
 const move:Job={...upload,operation:'move',source:{driveId:'files',path:'Documents/Folder'}};
 expect(jobAffectsLocation(move,{driveId:'files',path:'Documents'})).toBe(true);
 expect(jobAffectsLocation(move,{driveId:'files',path:'Documents/Folder/Nested'})).toBe(true);
 expect(jobAffectsLocation({...move,operation:'copy'},{driveId:'files',path:'Documents'})).toBe(false);
});
it('does not refresh storage for a download to the client',()=>{
 expect(jobAffectsLocation({...upload,operation:'download',source:{driveId:'files',path:'Music/song.mp3'},destination:null},{driveId:'files',path:'Music'})).toBe(false);
});
