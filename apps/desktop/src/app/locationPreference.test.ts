import {afterEach,it,expect} from 'vitest';
import {readLastLocation,saveLastLocation} from './locationPreference';
import {INITIAL_LOCATION,BACKUPS_DRIVE_ID} from '../fixtures/drives';
afterEach(()=>localStorage.clear());
it('restores a safe logical location preference',()=>{const location={driveId:BACKUPS_DRIVE_ID,path:'Backups'};saveLastLocation(location);expect(readLastLocation()).toEqual(location);});
it('falls back safely for corrupt or unsafe preference values',()=>{for(const value of ['not json',JSON.stringify({driveId:'unknown',path:''}),JSON.stringify({driveId:BACKUPS_DRIVE_ID,path:'../outside'})]){localStorage.setItem('archive-console:last-location:v1',value);expect(readLastLocation()).toEqual(INITIAL_LOCATION);}});
