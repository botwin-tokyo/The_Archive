import {archiveNameLabel} from '../../shared/lib/archiveLabels';
import { ArrowDown, ArrowUp, File, FileArchive, FileCode2, FileText, Folder, Link2, TriangleAlert } from 'lucide-react';
import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react';
import type { DirectoryQuery, Entry, OperationKind } from '../../contracts';
import { formatBytes, formatModified } from './format';
import { entryKey, selectRange } from './selection';

type Sort = NonNullable<DirectoryQuery['sort']>;
export interface FileTableProps {
  singleClickFolders?: boolean;
  entries: Entry[]; selected: Entry[]; disabled?: boolean; sort: Sort; direction: 'asc' | 'desc';
  onSort: (sort: Sort) => void; onSelection: (entries: Entry[]) => void; onOpen: (entry: Entry) => void;
  onOperation?: (kind: OperationKind, entries: Entry[]) => void;
}
export function EntryIcon({ entry }: { entry: Entry }) {
  if (entry.kind === 'directory') return <Folder size={18} className="files-icon-folder" fill="currentColor" fillOpacity=".1"/>;
  if (entry.kind === 'symlink') return <Link2 size={18} className="files-icon-link"/>;
  if (entry.kind === 'unsupported') return <TriangleAlert size={18} className="files-icon-unknown"/>;
  if (/\.(zip|tar|gz|7z|zst|bak)$/i.test(entry.name)) return <FileArchive size={18} className="files-icon-archive"/>;
  if (/\.(json|js|ts|py|sh|yml|yaml|toml)$/i.test(entry.name)) return <FileCode2 size={18} className="files-icon-code"/>;
  if (/\.(txt|md|log|pdf)$/i.test(entry.name)) return <FileText size={18} className="files-icon-document"/>;
  return <File size={18} className="files-icon-file"/>;
}
function typeLabel(entry: Entry) {
  if (entry.kind === 'directory') return 'Folder';
  if (entry.kind === 'symlink') return 'Symbolic link';
  if (entry.kind === 'unsupported') return 'Unsupported item';
  return entry.type || 'File';
}
export function FileTable({ singleClickFolders = false, entries, selected, disabled = false, sort, direction, onSort, onSelection, onOpen, onOperation }: FileTableProps) {
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const anchor = useRef<string | null>(null);
  const tableRef = useRef<HTMLTableElement>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; entries: Entry[] } | null>(null);
  const [widths, setWidths] = useState({ modified: 150, type: 126, size: 100 });
  const selectedKeys = new Set(selected.map(entryKey));
  const effectiveFocus = entries.some((entry) => entryKey(entry) === focusKey) ? focusKey : entries[0] && entryKey(entries[0]);
  useEffect(() => { setMenu(null); }, [entries, disabled]);
  const focusEntry = (entry: Entry) => {
    const key = entryKey(entry);
    setFocusKey(key);
    const row = [...(tableRef.current?.querySelectorAll<HTMLTableRowElement>('tbody tr') ?? [])].find((candidate) => candidate.dataset.entryKey === key);
    row?.focus();
  };
  const select = (entry: Entry, modifiers: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }) => {
    if (disabled) return;
    const key = entryKey(entry);
    if (modifiers.shiftKey) onSelection(selectRange(entries, anchor.current, entry));
    else if (modifiers.ctrlKey || modifiers.metaKey) {
      onSelection(selectedKeys.has(key) ? selected.filter((value) => entryKey(value) !== key) : [...selected, entry]);
      anchor.current = key;
    } else { onSelection([entry]); anchor.current = key; }
    setFocusKey(key);
  };
  const handleKey = (event: KeyboardEvent<HTMLTableRowElement>, entry: Entry, index: number) => {
    if (disabled || event.altKey) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1 : Math.min(entries.length - 1, Math.max(0, index + (event.key === 'ArrowDown' ? 1 : -1)));
      const next = entries[nextIndex];
      if (event.shiftKey) { if (!anchor.current) anchor.current = entryKey(entry); onSelection(selectRange(entries, anchor.current, next)); }
      else if (!event.ctrlKey && !event.metaKey) { onSelection([next]); anchor.current = entryKey(next); }
      focusEntry(next);
    } else if (event.key === 'Enter') { event.preventDefault(); onOpen(entry); }
    else if (event.key === ' ') { event.preventDefault(); select(entry, { ...event, ctrlKey: !event.shiftKey }); }
    else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') { event.preventDefault(); onSelection(entries); }
    else if (event.key === 'Escape') { event.preventDefault(); setMenu(null); onSelection([]); }
    else if (event.key === 'F2' && selected.length === 1 && selected[0].capabilities.rename) { event.preventDefault(); onOperation?.('rename', selected); }
    else if (event.key === 'Delete' && selected.length && selected.every((value) => value.capabilities.trash)) { event.preventDefault(); onOperation?.('trash', selected); }
    else if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
      event.preventDefault(); const bounds = event.currentTarget.getBoundingClientRect(); openContext(entry, bounds.left + 100, bounds.bottom);
    }
  };
  const openContext = (entry: Entry, x: number, y: number) => {
    if (disabled) return;
    const targets = selectedKeys.has(entryKey(entry)) ? selected : [entry];
    if (!selectedKeys.has(entryKey(entry))) onSelection(targets);
    setMenu({ entries: targets, x: Math.max(8, Math.min(x, window.innerWidth - 210)), y: Math.max(8, Math.min(y, window.innerHeight - 260)) });
  };
  const context = (event: MouseEvent, entry: Entry) => { event.preventDefault(); openContext(entry, event.clientX, event.clientY); };
  const operationAvailable = (kind: 'copy' | 'move' | 'download' | 'rename' | 'trash') => !!onOperation && !!menu?.entries.length && menu.entries.every((entry) => entry.capabilities[kind]) && (kind !== 'rename' || menu.entries.length === 1);
  return <>
    <table className="files-table" ref={tableRef} aria-label="Files in current folder" aria-multiselectable="true" aria-busy={disabled}>
      <colgroup><col/><col style={{ width: widths.modified }}/><col style={{ width: widths.type }}/><col style={{ width: widths.size }}/></colgroup>
      <thead><tr>{(['name', 'modified', 'type', 'size'] as const).map((column) => <th key={column} className={`files-column-${column}`} scope="col" aria-sort={sort === column ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}>
        <button className="files-sort" onClick={() => onSort(column)}>{column[0].toUpperCase() + column.slice(1)}{sort === column && (direction === 'asc' ? <ArrowUp size={13}/> : <ArrowDown size={13}/>)}</button>
        {column !== 'name' && <span role="separator" aria-label={`Resize ${column} column`} aria-orientation="vertical" aria-valuenow={widths[column]} tabIndex={0} className="files-column-resizer" onKeyDown={(event) => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); setWidths((current) => ({ ...current, [column]: Math.min(320, Math.max(80, current[column] + (event.key === 'ArrowRight' ? 10 : -10))) })); } }} onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); event.currentTarget.dataset.start = `${event.clientX},${widths[column]}`; }} onPointerMove={(event) => { if (!event.currentTarget.hasPointerCapture(event.pointerId)) return; const [start, width] = event.currentTarget.dataset.start!.split(',').map(Number); setWidths((current) => ({ ...current, [column]: Math.min(320, Math.max(80, width + event.clientX - start)) })); }} onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}/>}
      </th>)}</tr></thead>
      <tbody>{entries.map((entry, index) => <tr key={entryKey(entry)} data-entry-key={entryKey(entry)} aria-selected={selectedKeys.has(entryKey(entry))} className={selectedKeys.has(entryKey(entry)) ? 'is-selected' : ''} tabIndex={effectiveFocus === entryKey(entry) ? 0 : -1} onClick={(event) => { if (singleClickFolders && !disabled && entry.kind === 'directory' && !event.ctrlKey && !event.metaKey && !event.shiftKey) onOpen(entry); else select(entry, event); }} onFocus={() => setFocusKey(entryKey(entry))} onDoubleClick={() => !disabled && onOpen(entry)} onContextMenu={(event) => context(event, entry)} onKeyDown={(event) => handleKey(event, entry, index)}>
        <td className="files-cell-name"><span className="files-name-content"><input type="checkbox" aria-label={`Select ${archiveNameLabel(entry.name)}`} checked={selectedKeys.has(entryKey(entry))} disabled={disabled} tabIndex={-1} onClick={(event) => event.stopPropagation()} onChange={() => select(entry, { shiftKey: false, ctrlKey: true, metaKey: false })}/><EntryIcon entry={entry}/><bdi title={archiveNameLabel(entry.name)}>{archiveNameLabel(entry.name)}</bdi>{entry.kind === 'symlink' && <span className="files-inline-note">not followed</span>}</span></td>
        <td className="files-column-modified" title={entry.modifiedAt ?? 'Unknown'}>{formatModified(entry.modifiedAt)}</td><td className="files-column-type" title={typeLabel(entry)}>{typeLabel(entry)}</td><td className="files-column-size">{entry.kind === 'directory' ? '—' : formatBytes(entry.sizeBytes)}</td>
      </tr>)}</tbody>
    </table>
    {menu && <><div className="files-context-dismiss" onClick={() => setMenu(null)} onContextMenu={(event) => { event.preventDefault(); setMenu(null); }}/><div className="files-context-menu" role="menu" aria-label="Selection actions" style={{ left: menu.x, top: menu.y }} onKeyDown={(event) => { if (event.key === 'Escape') { setMenu(null); const first = menu.entries[0]; if (first) focusEntry(first); } if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]; const current = buttons.indexOf(document.activeElement as HTMLButtonElement); buttons[(current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus(); } }}>
      <span className="files-menu-heading">{menu.entries.length} selected</span>
      <button role="menuitem" autoFocus onClick={() => { onOpen(menu.entries[0]); setMenu(null); }}>Open details</button>
      {(['download', 'copy', 'move', 'rename', 'trash'] as const).map((kind) => <button role="menuitem" key={kind} disabled={!operationAvailable(kind)} title={!operationAvailable(kind) ? 'Unavailable for this selection' : undefined} className={kind === 'trash' ? 'files-danger' : ''} onClick={() => { onOperation?.(kind, menu.entries); setMenu(null); }}>{({ download: 'Download', copy: 'Copy to…', move: 'Move to…', rename: 'Rename…', trash: 'Move to Trash' })[kind]}</button>)}
    </div></>}
  </>;
}
