import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { domainError } from '../../contracts';
import type { DirectoryQuery, Entry, FileReadPort } from '../../contracts';
import { MusicAlbums } from './MusicAlbums';
const root = { driveId: 'files', path: 'Music' };
function folder(path: string): Entry {
  return { id: path, driveId: 'files', path, name: path.split('/').at(-1)!, parentPath: path.split('/').slice(0, -1).join('/'), revision: 1, kind: 'directory', type: 'Folder', sizeBytes: null, createdAt: null, modifiedAt: null, capabilities: { read: true, copy: true, move: true, rename: true, trash: true, download: true } };
}
afterEach(cleanup);
it('reads later artist pages, sorts by album title instead of year, searches artists and opens the original path', async () => {
  const artistA = folder('Music/Zed [0123456789abcdef]'), artistB = folder('Music/Amy [abcdef0123456789]');
  const alpha = folder(`${artistA.path}/2025 - Alpha [1234567890123456789012]`), beta = folder(`${artistB.path}/1990 - Beta [2234567890123456789012]`);
  const list = vi.fn(async (query: DirectoryQuery) => ({ schemaVersion: 1 as const, requestId: query.requestId, location: query.location, revision: 1, stale: false, total: query.location.path === 'Music' ? 2 : 1, entries: query.location.path === 'Music' ? [query.cursor ? artistB : artistA] : [query.location.path === artistA.path ? alpha : beta], nextCursor: query.location.path === 'Music' && !query.cursor ? 'second' : null }));
  const port: FileReadPort = { list, stat: vi.fn() }, onNavigate = vi.fn();
  render(<MusicAlbums port={port} location={root} onNavigate={onNavigate} onClose={vi.fn()}/>);
  await screen.findByText('Alpha');
  const rows = screen.getAllByRole('button').filter(button => button.classList.contains('music-album-row'));
  expect(rows.map(row => row.querySelector('strong')?.textContent)).toEqual(['Alpha', 'Beta']);
  fireEvent.click(rows[0]); expect(onNavigate).toHaveBeenCalledWith({ driveId: 'files', path: alpha.path });
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'amy' } });
  expect(screen.queryByText('Alpha')).toBeNull(); expect(screen.getByText('Beta')).toBeTruthy();
  expect(list).toHaveBeenCalledTimes(4);
});
it('finishes each artist before opening another and restarts expired pagination without retaining partial rows', async () => {
  const artists = ['A', 'B', 'C', 'D'].map(name => folder(`Music/${name}`));
  let expired = false, activeArtist = '', complete = true;
  const list = vi.fn(async (query: DirectoryQuery) => {
    const path = query.location.path;
    let entries: Entry[], nextCursor: string | null = null;
    if (path === 'Music') entries = artists;
    else {
      if (!query.cursor) {
        expect(complete || activeArtist === path).toBe(true);
        activeArtist = path; complete = false;
        entries = [folder(`${path}/${expired ? 'Fresh' : 'Obsolete'}`)]; nextCursor = 'next';
      } else {
        if (!expired) { expired = true; throw domainError('STALE_REVISION', 'Expired', true); }
        complete = true; entries = [folder(`${path}/Last`)];
      }
    }
    return { schemaVersion: 1 as const, requestId: query.requestId, location: query.location, revision: 1, stale: false, total: 4, entries, nextCursor };
  });
  render(<MusicAlbums port={{ list, stat: vi.fn() }} location={root} onNavigate={vi.fn()} onClose={vi.fn()}/>);
  await screen.findByText('8 albums');
  expect(screen.queryByText('Obsolete')).toBeNull();
  expect(screen.getAllByText('Fresh')).toHaveLength(4);
  expect(list).toHaveBeenCalledTimes(11);
});
