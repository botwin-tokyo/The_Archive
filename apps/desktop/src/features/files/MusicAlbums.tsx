import { useEffect, useState } from 'react';
import { Disc3, FolderOpen, RefreshCw } from 'lucide-react';
import { safeError, type Entry, type FileReadPort, type Location } from '../../contracts';
import { ScrollMore } from './ScrollMore';
import { archiveNameLabel } from '../../shared/lib/archiveLabels';

type Album = { entry: Entry; title: string; artist: string; year: string };
export function albumLabel(name: string): { title: string; year: string } {
  const label = archiveNameLabel(name);
  const match = /^(\d{4}(?:-\d{2}(?:-\d{2})?)?|Unknown) - (.+)$/.exec(label);
  return match ? { title: match[2], year: match[1] } : { title: label, year: '' };
}
async function directorySnapshot(port: FileReadPort, location: Location, signal: AbortSignal): Promise<Entry[]> {
  const result: Entry[] = [], seen = new Set<string>();
  let cursor: string | null = null;
  do {
    if (signal.aborted) return [];
    const requestId = crypto.randomUUID();
    const page = await port.list({ location, requestId, cursor, limit: 100, sort: 'name', direction: 'asc', foldersFirst: true, search: '', scope: 'directory' }, { signal });
    if (page.stale || page.requestId !== requestId || page.location.driveId !== location.driveId || page.location.path !== location.path) throw new Error('The music listing changed. Refresh albums to try again.');
    result.push(...page.entries.filter(entry => entry.kind === 'directory' && entry.capabilities.read));
    cursor = page.nextCursor;
    if (cursor && seen.has(cursor)) throw new Error('The music listing could not finish. Refresh albums to try again.');
    if (cursor) seen.add(cursor);
  } while (cursor);
  return result;
}
async function directories(port: FileReadPort, location: Location, signal: AbortSignal): Promise<Entry[]> {
  for (let attempt = 0; ; attempt++) {
    try { return await directorySnapshot(port, location, signal); }
    catch (reason: unknown) {
      if (signal.aborted) return [];
      const code = safeError(reason).code;
      // Restart only this folder, dropping partial pages from the expired snapshot.
      if (attempt >= 2 || (code !== 'STALE_REVISION' && code !== 'SOURCE_CHANGED')) throw reason;
    }
  }
}
export function MusicAlbums({ port, location, onNavigate, onClose }: { port: FileReadPort; location: Location; onNavigate: (location: Location) => void; onClose: () => void }) {
  const [albums, setAlbums] = useState<Album[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [retry, setRetry] = useState(0);
  const [visibleCount, setVisibleCount] = useState(100);
  const { driveId, path } = location;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    void (async () => {
      const artists = await directories(port, { driveId, path }, controller.signal);
      const result: Album[] = [];
      // The catalog retains only three snapshots per actor. Finish each folder
      // before opening another, leaving room for normal browser reads.
      for (const artist of artists) {
        if (controller.signal.aborted) return;
        const folders = await directories(port, { driveId, path: artist.path }, controller.signal);
        result.push(...folders.map(entry => ({ entry, artist: archiveNameLabel(artist.name), ...albumLabel(entry.name) })));
      }
      if (!controller.signal.aborted) {
        result.sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: 'base', numeric: true }) || a.artist.localeCompare(b.artist) || a.entry.path.localeCompare(b.entry.path));
        setAlbums(result); setVisibleCount(100);
      }
    })().catch((reason: unknown) => { if (!controller.signal.aborted) setError(safeError(reason).message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [port, driveId, path, retry]);
  const term = search.trim().toLocaleLowerCase();
  const matches = albums.filter(album => `${album.title} ${album.artist} ${album.year}`.toLocaleLowerCase().includes(term));
  const visible = matches.slice(0, visibleCount);
  return <section className="files-view music-albums" aria-label="Music albums">
    <div className="files-navigation-row"><button onClick={onClose}><FolderOpen size={16}/>Back to folders</button><h2>Music · Albums</h2></div>
    <div className="files-toolbar"><label>Find album or artist <input type="search" aria-label="Find album or artist" value={search} onChange={event => { setSearch(event.target.value); setVisibleCount(100); }}/></label><button disabled={loading} onClick={() => setRetry(value => value + 1)}><RefreshCw size={16}/>Refresh albums</button></div>
    <p className="music-albums-help">Albums A–Z across artist folders. Select an album to browse its files.</p>
    {loading && <p role="status">Loading albums…</p>}
    {error && <p role="alert">{error} Use Refresh albums to retry.</p>}
    <div className="files-list-scroll music-album-list">
      {visible.map(album => <button className="music-album-row" key={album.entry.path} disabled={!!error || !album.entry.capabilities.read} onClick={() => onNavigate({ driveId: album.entry.driveId, path: album.entry.path })}><Disc3 size={24}/><span><strong>{album.title}</strong><small>{album.artist}{album.year ? ` · ${album.year}` : ''}</small></span><FolderOpen size={18}/></button>)}
      {visibleCount < matches.length && <ScrollMore loading={false} onMore={() => setVisibleCount(value => value + 100)}/>}
      {!loading && !error && matches.length === 0 && <p>{search ? 'No matching albums.' : 'No album folders yet. Albums saved under Music → Artist → Album appear here.'}</p>}
    </div>
    <footer className="files-list-footer"><span>{matches.length} albums</span><span>{visible.length} shown · {visibleCount < matches.length ? "Scroll for more" : "All albums loaded"}</span></footer>
  </section>;
}
