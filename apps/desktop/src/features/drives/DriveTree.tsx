import {archiveNameLabel} from '../../shared/lib/archiveLabels';
import { ChevronDown, ChevronRight, Database, Folder, HardDrive, RefreshCw, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { safeError, type Drive, type Entry, type FileReadPort, type Location } from '../../contracts';
import { formatBytes } from '../files';
import { locationKey, sameLocation } from '../files';
import { driveStateLabel, readExpanded, saveExpanded, usagePercent } from './driveState';
import './features.css';

type Branch = { entries: Entry[]; loading: boolean; error: string | null; cursor: string | null; nextCursor: string | null };
export interface DriveTreeProps { drives: Drive[]; files: FileReadPort; location: Location; onNavigate: (location: Location) => void; onTrash?: (driveId: string) => void }
export function DriveTree({ drives, files, location, onNavigate, onTrash }: DriveTreeProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => readExpanded(location));
  const [branches, setBranches] = useState<Record<string, Branch>>({});
  const requests = useRef(new Map<string, { controller: AbortController; generation: number }>());
  const generation = useRef(0);
  const treeRef = useRef<HTMLDivElement>(null);
  const loadBranch = (target: Location, cursor: string | null = null) => {
    const key = locationKey(target);
    requests.current.get(key)?.controller.abort();
    const controller = new AbortController();
    const current = ++generation.current;
    requests.current.set(key, { controller, generation: current });
    setBranches((previous) => {
      const keys = Object.keys(previous);
      const next = { ...previous };
      if (keys.length >= 64 && !next[key]) { const disposable = keys.find((candidate) => !expanded.has(candidate)) ?? keys[0]; delete next[disposable]; }
      return { ...next, [key]: { entries: previous[key]?.entries ?? [], loading: true, error: null, cursor, nextCursor: null } };
    });
    const requestId = `tree-${current}`;
    void files.list({ location: target, requestId, cursor, limit: 100, sort: 'name', direction: 'asc', foldersFirst: true, scope: 'directory' }, { signal: controller.signal }).then((page) => {
      if (controller.signal.aborted || requests.current.get(key)?.generation !== current) return;
      if (locationKey(page.location) !== key || page.requestId !== requestId) throw new Error('Mismatched tree response');
      setBranches((previous) => ({ ...previous, [key]: { entries: page.entries.filter((entry) => entry.kind === 'directory').slice(0, 100), loading: false, error: page.stale ? 'Cached folders. Retry for current contents.' : null, cursor, nextCursor: page.entries.at(-1)?.kind === 'directory' ? page.nextCursor : null } }));
    }).catch((error: unknown) => {
      if (controller.signal.aborted || requests.current.get(key)?.generation !== current) return;
      setBranches((previous) => ({ ...previous, [key]: { entries: previous[key]?.entries ?? [], loading: false, error: safeError(error).message, cursor, nextCursor: null } }));
    }).finally(() => { if (requests.current.get(key)?.generation === current) requests.current.delete(key); });
  };
  useEffect(() => {
    for (const key of expanded) {
      let target: Location;
      try { const [driveId, path] = JSON.parse(key) as [string, string]; if (typeof driveId !== 'string' || typeof path !== 'string') continue; target = { driveId, path }; } catch { continue; }
      const drive = drives.find((value) => value.id === target.driveId);
      if (drive?.capabilities.browse && drive.state !== 'offline' && !branches[key] && !requests.current.has(key)) loadBranch(target);
    }
    saveExpanded(expanded);
  }, [expanded, drives, branches]);
  useEffect(() => { const active = requests.current; return () => { for (const request of active.values()) request.controller.abort(); active.clear(); }; }, [files]);
  const toggle = (target: Location) => {
    const key = locationKey(target);
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key); else { if (next.size >= 64) next.delete([...next][0]); next.add(key); }
      return next;
    });
  };
  const keyNavigation = (event: KeyboardEvent<HTMLElement>, target: Location, expandable = true) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === 'ArrowRight' && expandable) { event.preventDefault(); if (!expanded.has(locationKey(target))) toggle(target); }
    if (event.key === 'ArrowLeft' && expandable) { event.preventDefault(); if (expanded.has(locationKey(target))) toggle(target); else { const parent = event.currentTarget.parentElement?.closest<HTMLElement>('[role="treeitem"]'); parent?.focus(); } }
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onNavigate(target); }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault(); const rows = [...(treeRef.current?.querySelectorAll<HTMLElement>('[role="treeitem"]:not([aria-disabled="true"])') ?? [])]; const current = rows.indexOf(event.currentTarget); const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1 : Math.max(0, Math.min(rows.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1))); rows[next]?.focus();
    }
  };
  const renderBranch = (target: Location, depth: number, drive: Drive) => {
    const branch = branches[locationKey(target)];
    if (!branch) return null;
    const unavailable = drive.state === 'offline' || !drive.capabilities.browse;
    return <div role="group" className="drive-children">
      {branch.loading && <div className="drive-tree-note" role="status"><RefreshCw size={12}/>Loading folders…</div>}
      {branch.error && <div className="drive-tree-error"><span>{branch.error}</span><button disabled={unavailable} onClick={() => loadBranch(target, branch.cursor)}>Retry</button></div>}
      {!branch.loading && !branch.error && branch.entries.length === 0 && <div className="drive-tree-note">No folders</div>}
      {branch.entries.map((entry) => { const child = { driveId: entry.driveId, path: entry.path }; const isExpanded = expanded.has(locationKey(child)); return <div role="treeitem" key={entry.id} tabIndex={unavailable ? -1 : 0} aria-label={archiveNameLabel(entry.name)} aria-selected={sameLocation(location, child)} aria-expanded={isExpanded} aria-disabled={unavailable || !entry.capabilities.read} className="drive-tree-item" onKeyDown={(event) => !unavailable && keyNavigation(event, child)}>
        <div className={`drive-tree-row ${sameLocation(location, child) ? 'is-current' : ''}`} style={{ paddingLeft: 8 + depth * 13 }}><button className="drive-expand" tabIndex={-1} disabled={unavailable || !entry.capabilities.read} aria-label={`${isExpanded ? 'Collapse' : 'Expand'} ${archiveNameLabel(entry.name)}`} onClick={() => toggle(child)}>{isExpanded ? <ChevronDown size={12}/> : <ChevronRight size={12}/>}</button><button className="drive-folder-link" tabIndex={-1} disabled={unavailable || !entry.capabilities.read} title={archiveNameLabel(entry.name)} onClick={() => onNavigate(child)}><Folder size={14}/><bdi>{archiveNameLabel(entry.name)}</bdi></button></div>
        {isExpanded && renderBranch(child, depth + 1, drive)}
      </div>; })}
      {!branch.loading && branch.nextCursor && <button className="drive-tree-page" disabled={unavailable} onClick={() => loadBranch(target, branch.nextCursor)}>More folders…</button>}
      {!branch.loading && branch.cursor && <button className="drive-tree-page" disabled={unavailable} onClick={() => loadBranch(target)}>First folders</button>}
    </div>;
  };
  return <div className="drive-tree" ref={treeRef}><div className="drive-section-label"><span>Independent drives</span><span>{drives.length.toString().padStart(2, '0')}</span></div>{drives.map((drive, index) => {
    const root = { driveId: drive.id, path: '' }; const rootKey = locationKey(root); const open = expanded.has(rootKey); const usage = usagePercent(drive); const unavailable = drive.state === 'offline' || !drive.capabilities.browse;
    return <section className={`drive-section ${unavailable ? 'is-offline' : ''}`} key={drive.id} aria-label={drive.label}>
      <div className="drive-card"><div className="drive-title-line"><span className="drive-device-icon">{drive.role === 'files' ? <HardDrive size={19}/> : <Database size={19}/>}</span><button className="drive-title" title={drive.label} onClick={() => onNavigate(root)}><span>{drive.label}</span><small>DRIVE {String(index + 1).padStart(2, '0')} · {drive.role === 'files' ? 'FILE STORAGE' : 'BACKUP DESTINATION'}</small></button><button className="drive-expand" aria-label={`${open ? 'Collapse' : 'Expand'} ${drive.label}`} aria-expanded={open} disabled={unavailable} onClick={() => toggle(root)}>{open ? <ChevronDown size={15}/> : <ChevronRight size={15}/>}</button></div>
        <div className="drive-capacity"><span>{drive.capacityBytes === null ? 'Capacity unknown' : `${formatBytes(drive.freeBytes)} free / ${formatBytes(drive.capacityBytes)}`}</span><span className={`drive-state state-${drive.state}`}><i/>{driveStateLabel(drive)}</span></div><div className={`drive-usage ${usage === null ? 'is-unknown' : ''}`} role={usage !== null ? 'meter' : undefined} aria-label={`${drive.label} storage usage`} aria-valuemin={usage !== null ? 0 : undefined} aria-valuemax={usage !== null ? 100 : undefined} aria-valuenow={usage ?? undefined}>{usage !== null && <span style={{ width: `${Math.min(100, Math.max(0, usage))}%` }}/>}</div>
      </div>
      {open && <div role="tree" aria-label={`${drive.label} folders`}><div className="drive-tree-item" role="treeitem" aria-label={`${drive.label} root`} tabIndex={unavailable ? -1 : 0} aria-selected={sameLocation(location, root)} aria-expanded={true} aria-disabled={unavailable} onKeyDown={(event) => !unavailable && keyNavigation(event, root)}><div className={`drive-tree-row drive-root ${sameLocation(location, root) ? 'is-current' : ''}`}><button className="drive-folder-link" tabIndex={-1} disabled={unavailable} onClick={() => onNavigate(root)}><HardDrive size={14}/><span>All files</span></button></div>{renderBranch(root, 1, drive)}</div></div>}
      {unavailable && <p className="drive-offline-note">{drive.state === 'offline' ? 'Drive identity retained. Reconnect to browse.' : 'Configure this drive to browse.'}</p>}
      <button className="drive-trash" disabled={!onTrash} title={!onTrash ? 'Trash view is unavailable' : `${drive.label} Trash`} onClick={() => onTrash?.(drive.id)}><Trash2 size={14}/><span>Trash</span><ChevronRight size={12}/></button>
      {drive.role === 'harness-backups' && <p className="drive-destination-note">Schedule managed by Memory Harness</p>}
    </section>;
  })}<div className="drive-independence-note"><span className="drive-independence-icon">{drives.length}</span><span>Independent volumes<br/><small>No automatic mirroring</small></span></div></div>;
}
