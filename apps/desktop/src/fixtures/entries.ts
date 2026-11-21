import type { Entry } from '../contracts';
import { BACKUPS_DRIVE_ID, FILES_DRIVE_ID, FIXTURE_TIME } from './drives';
export function createEntry(id: string, driveId: string, path: string, kind: Entry['kind'] = 'file', sizeBytes: string | null = '2048'): Entry {
  const name = path.split('/').at(-1) ?? path;
  const extension = name.includes('.') ? name.split('.').at(-1)?.toUpperCase() : undefined;
  const transferable = kind === 'file' || kind === 'directory';
  return { id, driveId, path, revision: 1, name, parentPath: path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '', kind, type: kind === 'directory' ? 'Folder' : kind === 'symlink' ? 'Symbolic link' : kind === 'unsupported' ? 'Unsupported entry' : extension ? `${extension} file` : 'File', sizeBytes: kind === 'directory' ? null : sizeBytes, createdAt: FIXTURE_TIME, modifiedAt: FIXTURE_TIME, capabilities: { read: transferable, download: transferable, copy: transferable, move: transferable, rename: transferable, trash: transferable, ...(!transferable ? { reason: 'Links and special entries are not followed in this demo' } : {}) } };
}
export function createEntries(): Entry[] {
  const folders = ['Archive', 'Archive/Audio', 'Archive/Documents', 'Archive/Photography', 'Archive/Projects', 'Incoming', 'Shared', 'Empty folder', 'Large directory'];
  const entries = folders.map((path, i) => createEntry(`folder-${i}`, FILES_DRIVE_ID, path, 'directory'));
  const files: [string, string][] = [
    ['Archive/Field recordings.wav', '184549376'], ['Archive/Console study.png', '12582912'], ['Archive/Project notes.md', '12560'], ['Archive/Session archive.tar', '4294967296'], ['Archive/Signal atlas.pdf', '8642560'], ['Archive/Prototype.blend', '78643200'], ['Archive/Read me.txt', '3072'], ['Archive/Untitled data.bin', '9437184'], ['Archive/Empty file.dat', '0'], ['Archive/日本語のメモ.txt', '8192'], ['Archive/A very long synthetic filename that remains readable in the details panel and is never truncated for an operation.flac', '245366784'], ['Archive/Audio/Ambient session 01.flac', '268435456'], ['Archive/Audio/Calibration tone.wav', '16777216'], ['Archive/Documents/Archive inventory.csv', '210944'], ['Archive/Photography/Frame 001.dng', '38797312'], ['Archive/Projects/Workbench.zip', '536870912'], ['Incoming/Incoming package.bin', '2147483648'], ['Shared/Reference sheet.pdf', '3048576'],
  ];
  entries.push(...files.map(([path, size], i) => createEntry(`file-${i}`, FILES_DRIVE_ID, path, 'file', size)));
  entries.push(createEntry('harness-snapshots', BACKUPS_DRIVE_ID, 'Snapshots', 'directory'), createEntry('harness-staging', BACKUPS_DRIVE_ID, 'Incoming', 'directory'), createEntry('harness-file-1', BACKUPS_DRIVE_ID, 'Snapshots/harness-demo-001.tar', 'file', '6442450944'), createEntry('harness-manifest', BACKUPS_DRIVE_ID, 'Snapshots/transfer-manifest.json', 'file', '8192'));
  return entries;
}
export function largeEntry(index: number): Entry { const entry = createEntry(`large-${index}`, FILES_DRIVE_ID, `Large directory/Synthetic item ${String(index + 1).padStart(6, '0')}.bin`, 'file', String((index + 1) * 1024)); return { ...entry, capabilities: { read: true, download: false, copy: false, move: false, rename: false, trash: false, reason: 'Generated stress-test entries are metadata-only; use regular demo entries for operations' } }; }
