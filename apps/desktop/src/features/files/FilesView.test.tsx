import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { domainError, type DirectoryPage, type DirectoryQuery, type Entry, type FileReadPort, type Location, type ReadOptions } from '../../contracts';
import { FilesView } from './FilesView';
import { reconcileSelection } from './selection';
const root: Location = { driveId: 'drive-one', path: '' };
function entry(id: string, overrides: Partial<Entry> = {}): Entry { return { id, driveId: 'drive-one', name: id, path: id, parentPath: '', revision: 1, kind: 'file', type: 'File', sizeBytes: '1234', createdAt: null, modifiedAt: null, capabilities: { read: true, copy: true, move: true, rename: true, trash: true, download: true }, ...overrides }; }
function page(query: DirectoryQuery, entries: Entry[], overrides: Partial<DirectoryPage> = {}): DirectoryPage { return { schemaVersion: 1, requestId: query.requestId, location: query.location, revision: 1, entries, total: entries.length, stale: false, nextCursor: null, ...overrides }; }
function portWith(entries: Entry[]): FileReadPort { return { list: vi.fn(async (query) => page(query, query.direction === 'desc' ? [...entries].reverse() : entries)), stat: vi.fn(async (ref) => entries.find((value) => value.id === ref.id)!) }; }
afterEach(() => { cleanup(); localStorage.clear(); });
describe('Files browser request isolation', () => {
  it('rejects an older location returned last even if abort is ignored', async () => {
    const pending: { query: DirectoryQuery; signal?: AbortSignal; resolve: (value: DirectoryPage) => void }[] = [];
    const port: FileReadPort = { list: vi.fn((query: DirectoryQuery, options?: ReadOptions) => new Promise<DirectoryPage>((resolve) => pending.push({ query, signal: options?.signal, resolve }))), stat: vi.fn() };
    const props = { port, onNavigate: vi.fn(), onSelection: vi.fn() }; const view = render(<FilesView {...props} location={root}/>);
    view.rerender(<FilesView {...props} location={{ ...root, path: 'new' }}/>); await waitFor(() => expect(pending).toHaveLength(2));
    await act(async () => pending[1].resolve(page(pending[1].query, [entry('new-result')]))); await act(async () => pending[0].resolve(page(pending[0].query, [entry('obsolete-result')])));
    expect(screen.queryByText('obsolete-result')).toBeNull(); expect(screen.getByText('new-result')).toBeTruthy(); expect(pending[0].signal?.aborted).toBe(true);
  });
  it('cancels superseded scoped name searches', async () => {
    const pending: { query: DirectoryQuery; resolve: (value: DirectoryPage) => void }[] = [];
    const port: FileReadPort = { list: vi.fn((query: DirectoryQuery) => new Promise<DirectoryPage>((resolve) => pending.push({ query, resolve }))), stat: vi.fn() };
    render(<FilesView port={port} location={root} onNavigate={vi.fn()} onSelection={vi.fn()}/>);
    const search = screen.getByRole('textbox', { name: 'Search names in this folder' }); fireEvent.change(search, { target: { value: 'old' } }); fireEvent.change(search, { target: { value: 'new' } });
    await waitFor(() => expect(pending).toHaveLength(3)); await act(async () => pending[2].resolve(page(pending[2].query, [entry('new-match')]))); await act(async () => pending[1].resolve(page(pending[1].query, [entry('old-match')])));
    expect(screen.queryByText('old-match')).toBeNull(); expect(screen.getByText('new-match')).toBeTruthy(); expect(pending[2].query.scope).toBe('directory');
  });
  it('appends directory batches without removing earlier entries', async () => {
    const list = vi.fn(async (query: DirectoryQuery) => { const start = Number(query.cursor ?? 0); return page(query, Array.from({ length: 100 }, (_, index) => entry(`entry-${start + index}`)), { total: 100_000, nextCursor: String(start + 100) }); });
    render(<FilesView port={{ list, stat: vi.fn() }} location={root} onNavigate={vi.fn()} onSelection={vi.fn()}/>);
    await screen.findByText('entry-99'); expect(screen.getAllByRole('row')).toHaveLength(101); expect(list.mock.calls[0][0].limit).toBe(100); fireEvent.click(screen.getByRole('button', { name: 'Load more' })); await screen.findByText('entry-199'); expect(screen.getByText('entry-0')).toBeTruthy(); expect(screen.getAllByRole('row')).toHaveLength(201); expect(screen.getByText('200 loaded · Scroll for more')).toBeTruthy();
  });
});
describe('stable selection and keyboard', () => {
  it('retains stable selections through sorting but removes replaced revisions', async () => {
    let entries = [entry('alpha'), entry('beta')]; const list = vi.fn(async (query: DirectoryQuery) => page(query, query.direction === 'desc' ? [...entries].reverse() : [...entries])); const selection = vi.fn(); const props = { port: { list, stat: vi.fn() }, location: root, onNavigate: vi.fn(), onSelection: selection };
    const view = render(<FilesView {...props}/>); fireEvent.click(await screen.findByText('alpha')); await waitFor(() => expect(selection.mock.lastCall?.[0][0].id).toBe('alpha')); fireEvent.click(screen.getByRole('button', { name: 'Name' })); await waitFor(() => expect(list).toHaveBeenCalledTimes(2)); await waitFor(() => expect(screen.getByText('alpha').closest('tr')?.getAttribute('aria-selected')).toBe('true'));
    entries = [entry('alpha', { revision: 2 }), entry('beta')]; view.rerender(<FilesView {...props} refreshKey={1}/>); await waitFor(() => expect(list).toHaveBeenCalledTimes(3)); await waitFor(() => expect(selection.mock.lastCall?.[0]).toEqual([]));
  });
  it('supports range, page-scoped Ctrl+A, F2 and safe Delete', async () => {
    const entries = [entry('alpha'), entry('beta'), entry('gamma')]; const selection = vi.fn(); const operation = vi.fn(); render(<FilesView port={portWith(entries)} location={root} onNavigate={vi.fn()} onSelection={selection} onOperation={operation}/>);
    const alpha = (await screen.findByText('alpha')).closest('tr')!; fireEvent.click(alpha); fireEvent.keyDown(alpha, { key: 'F2' }); expect(operation).toHaveBeenLastCalledWith('rename', [entries[0]]); fireEvent.keyDown(alpha, { key: 'ArrowDown', shiftKey: true }); expect(screen.getByText('beta').closest('tr')?.getAttribute('aria-selected')).toBe('true'); expect(alpha.getAttribute('aria-selected')).toBe('true');
    const beta = screen.getByText('beta').closest('tr')!; fireEvent.keyDown(beta, { key: 'a', ctrlKey: true }); await waitFor(() => expect(selection.mock.lastCall?.[0]).toHaveLength(3)); fireEvent.keyDown(beta, { key: 'Delete' }); expect(operation.mock.lastCall?.[0]).toBe('trash'); expect(operation.mock.lastCall?.[1]).toHaveLength(3); fireEvent.keyDown(beta, { key: 'Escape' }); await waitFor(() => expect(selection.mock.lastCall?.[0]).toEqual([]));
  });
  it('treats opaque files and symbolic links as metadata on Enter', async () => {
    const navigate = vi.fn(); const entries = [entry('<script>alert(1)</script>.bin'), entry('external-link', { kind: 'symlink' })]; render(<FilesView port={portWith(entries)} location={root} onNavigate={navigate} onSelection={vi.fn()}/>);
    fireEvent.keyDown((await screen.findByText(entries[0].name)).closest('tr')!, { key: 'Enter' }); fireEvent.keyDown(screen.getByText('external-link').closest('tr')!, { key: 'Enter' }); expect(navigate).not.toHaveBeenCalled(); expect(document.querySelector('script')).toBeNull();
  });
  it('supports Back, Forward and Up', async () => {
    const port: FileReadPort = { list: vi.fn(async (query) => page(query, query.location.path ? [] : [entry('documents', { kind: 'directory' })])), stat: vi.fn() };
    function Browser() { const [location, navigate] = useState(root); return <FilesView port={port} location={location} onNavigate={navigate} onSelection={vi.fn()}/>; }
    render(<Browser/>); fireEvent.doubleClick(await screen.findByText('documents')); await screen.findByText('This folder is empty'); fireEvent.click(screen.getByRole('button', { name: 'Back' })); await waitFor(() => expect(screen.getAllByText('documents').some((node) => node.closest('tr'))).toBe(true)); fireEvent.click(screen.getByRole('button', { name: 'Forward' })); await screen.findByText('This folder is empty'); fireEvent.click(screen.getByRole('button', { name: 'Up one folder' })); await waitFor(() => expect(screen.getAllByText('documents').some((node) => node.closest('tr'))).toBe(true));
  });
  it('offers drive-root recovery for missing folders', async () => {
    const navigate = vi.fn(); const port: FileReadPort = { list: vi.fn(async () => { throw domainError('NOT_FOUND', 'This folder has moved.'); }), stat: vi.fn() }; render(<FilesView port={port} location={{ ...root, path: 'missing' }} onNavigate={navigate} onSelection={vi.fn()}/>); fireEvent.click(await screen.findByRole('button', { name: 'Go to drive root' })); expect(navigate).toHaveBeenCalledWith(root);
  });
  it('prunes disappeared and replacement identities', () => { expect(reconcileSelection([entry('one'), entry('two'), entry('three')], [entry('one'), entry('two', { revision: 2 })])).toEqual([entry('one')]); });
});

describe('background download refreshes', () => {
  it('keeps folder navigation available while refreshing but gates mutations', async () => {
    const pending: {query:DirectoryQuery;resolve:(value:DirectoryPage)=>void}[]=[];
    const port:FileReadPort={list:vi.fn(query=>new Promise<DirectoryPage>(resolve=>pending.push({query,resolve}))),stat:vi.fn()};
    const navigate=vi.fn(),operation=vi.fn();
    const props={port,location:root,onNavigate:navigate,onSelection:vi.fn(),onOperation:operation};
    const view=render(<FilesView {...props}/>);
    await act(async()=>pending[0].resolve(page(pending[0].query,[entry('Music',{kind:'directory'})])));
    const row=screen.getByText('Music').closest('tr')!;
    fireEvent.click(row);
    view.rerender(<FilesView {...props} refreshKey={1}/>);
    expect((screen.getByRole('button',{name:'New folder'}) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Music').closest('tr')).toBe(row);
    expect((screen.getByRole('checkbox',{name:'Select Music'}) as HTMLInputElement).disabled).toBe(false);
    fireEvent.doubleClick(row);
    expect(navigate).toHaveBeenCalledWith({...root,path:'Music'});
    fireEvent.keyDown(row,{key:'Enter'});
    expect(navigate).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(row,{key:'Delete'});
    expect(operation).not.toHaveBeenCalled();
  });
  it('coalesces completion bursts without aborting the pending read or losing navigation isolation', async () => {
    const pending:{query:DirectoryQuery;signal?:AbortSignal;resolve:(value:DirectoryPage)=>void}[]=[];
    const port:FileReadPort={list:vi.fn((query,options)=>new Promise<DirectoryPage>(resolve=>pending.push({query,signal:options?.signal,resolve}))),stat:vi.fn()};
    const props={port,location:root,onNavigate:vi.fn(),onSelection:vi.fn()};
    const view=render(<FilesView {...props}/>);
    await act(async()=>pending[0].resolve(page(pending[0].query,[entry('Music',{kind:'directory'})])));
    for(let refreshKey=1;refreshKey<=6;refreshKey++)view.rerender(<FilesView {...props} refreshKey={refreshKey}/>);
    expect(pending).toHaveLength(2);expect(pending[1].signal?.aborted).toBe(false);
    await act(async()=>pending[1].resolve(page(pending[1].query,[entry('Music',{kind:'directory'}),entry('new-file')])));
    expect(pending).toHaveLength(3);expect(screen.getByText('new-file')).toBeTruthy();
    view.rerender(<FilesView {...props} location={{...root,path:'Music'}} refreshKey={6}/>);
    expect(pending[2].signal?.aborted).toBe(true);
    await act(async()=>pending[3].resolve(page(pending[3].query,[entry('song.mp3')])));
    await act(async()=>pending[2].resolve(page(pending[2].query,[entry('obsolete-root-file')])));
    expect(screen.queryByText('obsolete-root-file')).toBeNull();expect(screen.getByText('song.mp3')).toBeTruthy();
  });
  it('continues to disable stale entries after a failed background refresh', async () => {
    const navigate=vi.fn();const port=portWith([entry('Music',{kind:'directory'})]);
    const props={port,location:root,onNavigate:navigate,onSelection:vi.fn()};const view=render(<FilesView {...props}/>);
    await screen.findByText('Music');vi.mocked(port.list).mockRejectedValueOnce(domainError('UNAVAILABLE','Drive disconnected'));
    view.rerender(<FilesView {...props} refreshKey={1}/>);await screen.findByRole('alert');
    fireEvent.doubleClick(screen.getByText('Music'));
    expect(navigate).not.toHaveBeenCalled();
  });
});


describe('columns navigation', () => {
  it('opens folders with one click, keeps sibling navigation, and uses original paths', async () => {
    const music = entry('Music', {kind: 'directory'});
    const photos = entry('Photos', {kind: 'directory'});
    const albumName = 'Album [1234567890123456789012]';
    const album = entry(albumName, {kind: 'directory', path: `Music/${albumName}`, parentPath: 'Music'});
    const song = entry('Song.mp3', {path: `Music/${albumName}/Song.mp3`, parentPath: `Music/${albumName}`});
    const port: FileReadPort = {list: vi.fn(async query => page(query, query.location.path === '' ? [music, photos] : query.location.path === 'Music' ? [album] : query.location.path === album.path ? [song] : [])), stat: vi.fn()};
    const selection = vi.fn();
    function Browser() { const [location, navigate] = useState(root); return <FilesView port={port} location={location} onNavigate={navigate} onSelection={selection}/>; }
    render(<Browser/>);
    fireEvent.click(screen.getByRole('button', {name: 'Columns'}));
    fireEvent.click(await screen.findByText('Music', {selector: 'bdi'}));
    fireEvent.click(await screen.findByText('Album', {selector: 'bdi'}));
    fireEvent.click(await screen.findByText('Song.mp3'));
    await waitFor(() => expect(selection.mock.lastCall?.[0]).toEqual([song]));
    expect(screen.getByRole('region', {name: 'Parent folder drive-one'})).toBeTruthy();
    expect(screen.getByRole('region', {name: 'Parent folder Music'})).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: 'Photos'}));
    await screen.findByText('This folder is empty');
    expect(screen.queryByText('Song.mp3')).toBeNull();
    expect(screen.queryByRole('region', {name: 'Parent folder Music'})).toBeNull();
    expect(localStorage.getItem('archive.files.view')).toBe('columns');
    fireEvent.click(screen.getByRole('button', {name: 'Details'}));
    expect(screen.queryByLabelText('Folder columns')).toBeNull();
  });
  it('follows small parent batches and blocks stale parent navigation', async () => {
    const navigate = vi.fn();
    let stale = false;
    const list = vi.fn(async (query: DirectoryQuery) => page(query, query.location.path ? [] : [entry(query.cursor ? 'Other' : 'Music', {kind: 'directory'})], {nextCursor: query.location.path || query.cursor ? null : 'second', stale}));
    const props = {port: {list, stat: vi.fn()}, location: {...root, path: 'Music'}, onNavigate: navigate, onSelection: vi.fn()};
    const view = render(<FilesView {...props}/>);
    fireEvent.click(screen.getByRole('button', {name: 'Columns'}));
    await screen.findByRole('button', {name: 'Music'});
    const other = await screen.findByRole('button', {name: 'Other'});
    fireEvent.click(other);
    expect(navigate).toHaveBeenCalledWith({...root, path: 'Other'});
    stale = true;
    view.rerender(<FilesView {...props} refreshKey={1}/>);
    await waitFor(() => expect((other as HTMLButtonElement).disabled).toBe(true));
  });
});

it('recovers an evicted scroll cursor and refreshes to the loaded depth', async () => {
  let expired = false;
  const list = vi.fn(async (query: DirectoryQuery) => {
    if (query.cursor && !expired) { expired = true; throw domainError('STALE_REVISION', 'Snapshot evicted'); }
    const start = query.cursor ? 100 : 0;
    return page(query, Array.from({ length: 100 }, (_, index) => entry(`item-${start + index}`)), { total: 200, nextCursor: start ? null : 'next' });
  });
  const props = { port: { list, stat: vi.fn() }, location: root, onNavigate: vi.fn(), onSelection: vi.fn() };
  const view = render(<FilesView {...props}/>);
  await screen.findByText('item-99');
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
  await screen.findByText('item-199');
  expect(screen.getAllByRole('row')).toHaveLength(201);
  expect(screen.queryByRole('alert')).toBeNull();
  view.rerender(<FilesView {...props} refreshKey={1}/>);
  await waitFor(() => expect(list).toHaveBeenCalledTimes(6));
  await waitFor(() => expect(screen.getByText('200 loaded · All items loaded')).toBeTruthy());
  expect(screen.getByText('item-0')).toBeTruthy();
  expect(screen.getByText('item-199')).toBeTruthy();
});
