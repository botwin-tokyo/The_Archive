import { ArrowDownToLine, ArrowLeft, ArrowRight, ArrowUp, ChevronDown, Copy, FolderOpen, FolderPlus, MoreHorizontal, RefreshCw, Search, Trash2, Upload, X } from 'lucide-react';
import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import type { DirectoryQuery, Drive, Entry, FileReadPort, Location, OperationKind } from '../../contracts';
import { Breadcrumbs } from './Breadcrumbs';
import { FileTable } from './FileTable';
import { MusicAlbums } from './MusicAlbums';
import { FolderColumns } from './FolderColumns';
import { SearchBar } from './SearchBar';
import { locationKey, parentLocation, visit, type BrowserHistory } from './history';
import { reconcileSelection } from './selection';
import { ScrollMore } from './ScrollMore';
import { useDirectory } from './useDirectory';
import './features.css';

export interface FilesViewProps {
  port: FileReadPort; location: Location; onNavigate: (location: Location) => void;
  onSelection: (entries: Entry[]) => void; onOperation?: (kind: OperationKind, entries: Entry[]) => void;
  refreshKey?: number; drive?: Drive;
}
export function FilesView({ port, location, onNavigate, onSelection, onOperation, refreshKey = 0, drive }: FilesViewProps) {
  const [albumView, setAlbumView] = useState(false);
  const musicRoot = location.driveId === 'files' && location.path === 'Music';
  const [viewMode, setViewMode] = useState<'details' | 'columns'>(() => { try { return localStorage.getItem('archive.files.view') === 'columns' ? 'columns' : 'details'; } catch { return 'details'; } });
  const [sort, setSort] = useState<NonNullable<DirectoryQuery['sort']>>('name');
  const [direction, setDirection] = useState<'asc' | 'desc'>('asc');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState<Entry[]>([]);
  const [retry, setRetry] = useState(0);
  const [history, setHistory] = useState<BrowserHistory>({ locations: [location], index: 0 });
  const [more, setMore] = useState(false);
  const [pathVisible, setPathVisible] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const selectionCallback = useRef(onSelection);
  selectionCallback.current = onSelection;
  const key = locationKey(location);
  const { page, loading, refreshing, error, loadMore } = useDirectory({ port, location, sort, direction, search, refreshKey: refreshKey + retry });
  const visiblePage = page && locationKey(page.location) === key ? page : null;
  const entries = visiblePage?.entries ?? [];
  const disabled = (loading && !refreshing) || !!error || !!visiblePage?.stale || !!drive && (!drive.capabilities.browse || drive.state === 'offline' || drive.state === 'unconfigured');
  const canWrite = !!onOperation && !loading && !disabled && (!drive || drive.capabilities.write);
  const unavailableReason = loading ? 'Wait for the folder to load' : error || visiblePage?.stale ? 'Refresh the stale listing before making changes' : drive && !drive.capabilities.write ? `This drive is ${drive.state === 'read-only' ? 'read only' : 'not writable'}` : !onOperation ? 'Operations are unavailable in this view' : '';
  useEffect(() => { setHistory((current) => visit(current, location)); setSearch(''); setSelected([]); setMore(false); setPathVisible(false); }, [key]);
  useEffect(() => { selectionCallback.current(selected); }, [selected]);
  useEffect(() => { if (visiblePage && !loading) setSelected((previous) => { const next = reconcileSelection(previous, visiblePage.entries); return next.length === previous.length && next.every((entry, index) => entry === previous[index]) ? previous : next; }); }, [visiblePage, loading]);
  const changeSort = (next: NonNullable<DirectoryQuery['sort']>) => { setDirection(next === sort ? direction === 'asc' ? 'desc' : 'asc' : 'asc'); setSort(next); };
  const changeSearch = (value: string) => { setSearch(value); setSelected([]); };
  const goHistory = (offset: number) => { const index = history.index + offset; if (index < 0 || index >= history.locations.length) return; setHistory({ ...history, index }); onNavigate(history.locations[index]); };
  const openEntry = (entry: Entry) => { if (entry.kind === 'directory' && entry.capabilities.read) onNavigate({ driveId: entry.driveId, path: entry.path }); else setSelected([entry]); };
  const keyboard = (event: KeyboardEvent<HTMLElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') { event.preventDefault(); searchRef.current?.focus(); }
    if (event.altKey && event.key === 'ArrowLeft') { event.preventDefault(); goHistory(-1); }
    if (event.altKey && event.key === 'ArrowRight') { event.preventDefault(); goHistory(1); }
    if (event.altKey && event.key === 'ArrowUp') { event.preventDefault(); if (location.path) onNavigate(parentLocation(location)); }
    if (event.key === 'Escape') { setMore(false); setPathVisible(false); }
  };
  const allCan = (kind: 'copy' | 'move' | 'download' | 'rename' | 'trash') => !!onOperation && !loading && !disabled && selected.length > 0 && selected.every((entry) => entry.capabilities[kind]) && (kind !== 'rename' || selected.length === 1);
  const listing = <div className="files-list-scroll" data-testid="files-scroll">
      <FileTable entries={entries} selected={selected} disabled={disabled} sort={sort} direction={direction} singleClickFolders={viewMode === 'columns'} onSort={changeSort} onSelection={setSelected} onOpen={openEntry} onOperation={loading ? undefined : onOperation}/>
      {loading && !refreshing && <div className={`files-loading ${entries.length ? 'with-entries' : ''}`} role="status"><RefreshCw size={17}/>Loading folder…</div>}
      {visiblePage?.nextCursor && !error && !visiblePage.stale && <ScrollMore loading={loading} onMore={loadMore}/>}
      {!loading && !error && entries.length === 0 && <div className="files-empty"><FolderOpen size={38}/><h2>{search ? 'No matching names' : 'This folder is empty'}</h2><p>{search ? 'Try another name. Search is limited to this folder.' : 'A clean place for your next archive.'}</p>{search ? <button onClick={() => changeSearch('')}>Clear search</button> : <button disabled={!canWrite} onClick={() => onOperation?.('new-folder', [])}><FolderPlus size={15}/>New folder</button>}</div>}
    </div>;
  if (musicRoot && albumView) return <MusicAlbums port={port} location={location} onNavigate={onNavigate} onClose={() => setAlbumView(false)}/>;
  return <section className="files-view" aria-label="File browser" onKeyDown={keyboard}>
    <div className="files-navigation-row">
      <div className="files-history" aria-label="Folder history"><button aria-label="Back" title="Back (Alt+Left)" disabled={history.index === 0} onClick={() => goHistory(-1)}><ArrowLeft size={16}/></button><button aria-label="Forward" title="Forward (Alt+Right)" disabled={history.index === history.locations.length - 1} onClick={() => goHistory(1)}><ArrowRight size={16}/></button><button aria-label="Up one folder" title="Up (Alt+Up)" disabled={!location.path} onClick={() => onNavigate(parentLocation(location))}><ArrowUp size={16}/></button></div>
      <Breadcrumbs location={location} label={drive?.label ?? location.driveId} onNavigate={onNavigate}/>
      <SearchBar value={search} onChange={changeSearch} inputRef={searchRef}/>
    </div>
    <div className="files-toolbar">
      <div className="files-main-actions"><button className="files-primary" disabled={!canWrite} title={!canWrite ? unavailableReason : (drive?.simulated?'Select simulated upload files':'Choose local upload files')} onClick={() => onOperation?.('upload', [])}><Upload size={15}/>Upload</button><button className="files-secondary" disabled={!canWrite} title={!canWrite ? unavailableReason : undefined} onClick={() => onOperation?.('new-folder', [])}><FolderPlus size={16}/>New folder</button><div className="files-more-wrap"><button className="files-icon-button" aria-label="More folder actions" aria-expanded={more} onClick={() => setMore(!more)}><MoreHorizontal size={20}/></button>{more && <div className="files-more-menu" role="menu" aria-label="Folder actions"><button role="menuitem" onClick={() => { setRetry((current) => current + 1); setMore(false); }}><RefreshCw size={14}/>Refresh folder</button><button role="menuitem" onClick={() => { setPathVisible(!pathVisible); setMore(false); }}><Copy size={14}/>Show logical path</button></div>}</div></div>
      <div className="files-view-options">{musicRoot && <button onClick={() => { setSelected([]); setAlbumView(true); }}>Albums</button>}<div className="files-view-toggle" aria-label="Browser view"><button aria-pressed={viewMode === 'details'} onClick={() => { setViewMode('details'); try { localStorage.setItem('archive.files.view', 'details'); } catch { /* Optional preference storage. */ } }}>Details</button><button aria-pressed={viewMode === 'columns'} onClick={() => { setViewMode('columns'); try { localStorage.setItem('archive.files.view', 'columns'); } catch { /* Optional preference storage. */ } }}>Columns</button></div><span className="files-toolbar-divider"/><label>Sort: <select aria-label="Sort files" value={`${sort}:${direction}`} onChange={(event) => { const [nextSort, nextDirection] = event.target.value.split(':'); setSort(nextSort as NonNullable<DirectoryQuery['sort']>); setDirection(nextDirection as 'asc' | 'desc'); }}><option value="name:asc">Name A–Z</option><option value="name:desc">Name Z–A</option><option value="modified:desc">Modified newest</option><option value="modified:asc">Modified oldest</option><option value="type:asc">Type A–Z</option><option value="type:desc">Type Z–A</option><option value="size:desc">Size largest</option><option value="size:asc">Size smallest</option></select><ChevronDown size={12}/></label></div>
    </div>
    {pathVisible && <div className="files-path-panel"><span>Logical path</span><input aria-label="Logical path" readOnly value={`${location.driveId}:/${location.path}`} onFocus={(event) => event.currentTarget.select()}/><button aria-label="Close logical path" onClick={() => setPathVisible(false)}><X size={14}/></button></div>}
    <div className="files-selection-slot">{selected.length > 0 && <div className="files-selection-toolbar"><span>{selected.length} selected</span><button disabled={!allCan('download')} onClick={() => onOperation?.('download', selected)}><ArrowDownToLine size={14}/>Download</button><button disabled={!allCan('copy')} onClick={() => onOperation?.('copy', selected)}><Copy size={14}/>Copy to</button><button disabled={!allCan('move')} onClick={() => onOperation?.('move', selected)}>Move to</button><button disabled={!allCan('rename')} onClick={() => onOperation?.('rename', selected)}>Rename</button><button className="files-danger" disabled={!allCan('trash')} onClick={() => onOperation?.('trash', selected)}><Trash2 size={14}/>Move to Trash</button><button aria-label="Clear selection" className="files-clear-selection" onClick={() => setSelected([])}><X size={14}/></button></div>}</div>
    {search && <div className="files-search-scope"><Search size={13}/>Names matching “{search}” in this folder only <button onClick={() => changeSearch('')}>Clear</button></div>}
    {error && <div className="files-error" role="alert"><strong>{error.code === 'NOT_FOUND' ? 'Folder no longer available' : 'Could not load this folder'}</strong><span>{error.message}{visiblePage ? ' Showing cached entries; actions are disabled.' : ''}</span><button onClick={() => setRetry((value) => value + 1)}>Retry</button>{error.code === 'NOT_FOUND' && <button onClick={() => onNavigate({ driveId: location.driveId, path: '' })}>Go to drive root</button>}</div>}
    {visiblePage?.stale && !error && <div className="files-stale" role="status">Cached listing · refresh before making changes<button onClick={() => setRetry((value) => value + 1)}>Refresh</button></div>}
    {viewMode === 'columns' ? <FolderColumns port={port} location={location} label={drive?.label ?? location.driveId} refreshKey={refreshKey + retry} unavailable={!!drive && (!drive.capabilities.browse || drive.state === 'offline' || drive.state === 'unconfigured')} sort={sort} direction={direction} onNavigate={onNavigate}>{listing}</FolderColumns> : listing}
    <footer className="files-list-footer"><span>{loading && !refreshing ? 'Loading…' : `${visiblePage?.total.toLocaleString() ?? '—'} ${search ? 'matches' : 'items'}${refreshing ? ' · Updating…' : ''}`}{selected.length > 0 && <span className="files-footer-selection"> · {selected.length} selected</span>}</span><span>{entries.length.toLocaleString()} loaded{visiblePage && !visiblePage.nextCursor ? " · All items loaded" : " · Scroll for more"}</span></footer>
  </section>;
}
