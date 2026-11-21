import type { Drive, Location } from '../contracts';
export const FILES_DRIVE_ID = 'demo-files';
export const BACKUPS_DRIVE_ID = 'demo-harness';
export const INITIAL_LOCATION: Location = { driveId: FILES_DRIVE_ID, path: 'Archive' };
export const FIXTURE_TIME = '2026-09-30T09:00:00.000Z';
export function createDrives(): Drive[] { return [
  { id: FILES_DRIVE_ID, role: 'files', label: 'File Storage', state: 'online', configured: true, capacityBytes: null, freeBytes: null, temperatureC: null, capabilities: { browse: true, write: true, trash: true, purge: true }, revision: 1, simulated: true },
  { id: BACKUPS_DRIVE_ID, role: 'harness-backups', label: 'Memory Harness Backups', state: 'online', configured: true, capacityBytes: null, freeBytes: null, temperatureC: null, capabilities: { browse: true, write: true, trash: true, purge: true }, revision: 1, simulated: true },
]; }
