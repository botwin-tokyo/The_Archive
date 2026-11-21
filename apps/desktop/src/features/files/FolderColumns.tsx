import { ChevronRight, Folder } from 'lucide-react';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { DirectoryQuery, FileReadPort, Location } from '../../contracts';
import { archiveNameLabel } from '../../shared/lib/archiveLabels';
import { locationKey } from './history';
import { ScrollMore } from './ScrollMore';
import { useDirectory } from './useDirectory';

type Props = { port: FileReadPort; location: Location; label: string; refreshKey: number; unavailable: boolean; sort: DirectoryQuery['sort']; direction: DirectoryQuery['direction']; onNavigate: (location: Location) => void; children: ReactNode };
function ParentColumn({ port, location, label, activePath, refreshKey, unavailable, sort, direction, onNavigate }: Omit<Props, 'children'> & { activePath: string }) {
  const [retry, setRetry] = useState(0);
  const { page, loading, refreshing, error, loadMore } = useDirectory({ port, location, sort, direction, search: '', refreshKey: refreshKey + retry });
  const disabled = unavailable || !!error || !!page?.stale || (loading && !refreshing);
  return <section className="files-folder-column" aria-label={`Parent folder ${label}`}>
    <button className="files-column-heading" onClick={() => onNavigate(location)}>{label}</button>
    <div className="files-parent-rows">
      {page?.entries.filter(entry => entry.kind === 'directory').map(entry => <button key={entry.id} className="files-folder-choice" aria-current={entry.path === activePath ? 'location' : undefined} disabled={disabled || !entry.capabilities.read} onClick={() => onNavigate({ driveId: entry.driveId, path: entry.path })} title={archiveNameLabel(entry.name)}><Folder size={17}/><bdi>{archiveNameLabel(entry.name)}</bdi><ChevronRight size={14}/></button>)}
      {page?.nextCursor && !error && !page.stale && <ScrollMore loading={loading} onMore={loadMore}/>}
      {loading && <p role="status">{refreshing ? 'Updating…' : 'Loading…'}</p>}
      {(error || page?.stale) && <div role="alert"><p>{error?.message ?? 'Listing needs refreshing.'}</p><button onClick={() => setRetry(value => value + 1)}>Retry</button></div>}
      {!loading && !error && !page?.entries.some(entry => entry.kind === 'directory') && <p>No folders</p>}
    </div>

  </section>;
}
export function FolderColumns(props: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const parts = props.location.path.split('/').filter(Boolean);
  const key = locationKey(props.location);
  useEffect(() => { const element = scroller.current; if (element) element.scrollLeft = element.scrollWidth; }, [key]);
  return <div className="files-columns" ref={scroller} aria-label="Folder columns">
    {parts.map((part, index) => <ParentColumn {...props} key={`${props.location.driveId}:${parts.slice(0, index).join('/')}:${props.sort}:${props.direction}`} location={{ driveId: props.location.driveId, path: parts.slice(0, index).join('/') }} label={index ? archiveNameLabel(parts[index - 1]) : props.label} activePath={parts.slice(0, index + 1).join('/')}/>)}
    <section className="files-folder-column files-current-column" aria-label="Current folder column"><div className="files-column-heading">{parts.length ? archiveNameLabel(parts[parts.length - 1]) : props.label}</div>{props.children}</section>
  </div>;
}
