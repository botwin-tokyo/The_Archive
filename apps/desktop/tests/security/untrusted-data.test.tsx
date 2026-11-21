import { cleanup, render, screen } from '@testing-library/react';
import { readFileSync, readdirSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { runInNewContext } from 'node:vm';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { configurationSchema, entrySchema, operationRequestSchema } from '../../src/contracts';
import { createMockBackend } from '../../src/adapters/mock';
import { FileTable } from '../../src/features/files/FileTable';
import { createEntry } from '../../src/fixtures/entries';
import { FILES_DRIVE_ID } from '../../src/fixtures/drives';

afterEach(cleanup);
describe('untrusted data boundaries', () => {
  it('renders script-, event-handler-, URL-, Unicode- and prototype-shaped filenames as inert text', () => {
    const names = ['<script>alert(1)</script>.txt', '<img src=x onerror=alert(1)>.bin', 'javascript:alert(1)', '__proto__', 'constructor', '日本語 archive \u202Etxt.bin'];
    const entries = names.map((name, index) => ({ ...createEntry(`unsafe-${index}`, FILES_DRIVE_ID, `opaque-${index}`), name }));
    render(<FileTable entries={entries} selected={[]} sort="name" direction="asc" onSort={vi.fn()} onSelection={vi.fn()} onOpen={vi.fn()}/>);
    for (const name of names) expect(screen.getByText(name)).toBeTruthy();
    expect(document.querySelectorAll('script,img,iframe,object,embed,a[href]')).toHaveLength(0);
    expect(document.querySelectorAll('[onerror],[onclick],[srcdoc]')).toHaveLength(0);
  });
  it('rejects prototype-shaped request fields and invalid paths without polluting objects', () => {
    const request = JSON.parse('{"requestId":"malicious","kind":"new-folder","name":"safe","destination":{"driveId":"drive-files","path":""},"__proto__":{"polluted":true}}');
    expect(operationRequestSchema.safeParse(request).success).toBe(false);
    for (const unsafe of ['/absolute', '../outside', 'nested/../../outside', 'nested\\outside', '.archive-internal', 'name\0tail']) {
      expect(entrySchema.safeParse(createEntry('bad', FILES_DRIVE_ID, unsafe)).success).toBe(false);
    }
    expect(({} as { polluted?: boolean }).polluted).toBeUndefined();
  });
  it.each(['javascript:alert(1)', 'data:text/html,<script>', 'file:///tmp/private', 'http://example.invalid', 'ws://example.invalid', 'wss://user:secret@example.invalid', 'wss://example.invalid?token=secret', 'wss://example.invalid#secret'])('rejects unsafe endpoint %s', async endpoint => {
    const backend = createMockBackend(); const settings = await backend.configuration.get();
    expect(configurationSchema.safeParse({ ...settings, health: { ...settings.health, enabled: true, endpoint } }).success).toBe(false); backend.dispose();
  });
  it('allows no executable renderer HTML or native/network imports in application source', () => {
    const files: string[] = [];
    const visit = (directory: string) => { for (const item of readdirSync(directory, { withFileTypes: true })) { const filename = path.join(directory, item.name); if (item.isDirectory()) visit(filename); else if (/\.[jt]sx?$/.test(filename) && !filename.includes('.test.')) files.push(filename); } };
    visit('apps/desktop/src'); expect(files.length).toBeGreaterThan(20);
    for (const filename of files) {
      const source = readFileSync(filename, 'utf8');
      expect(source, filename).not.toMatch(/dangerouslySetInnerHTML|\.innerHTML\s*=|eval\s*\(|new Function\s*\(/);
      expect(source, filename).not.toMatch(/(?:from\s*|import\s*\(|require\s*\()\s*['"](?:node:|electron|fs['"]|child_process)/);
      expect(source, filename).not.toMatch(/\b(?:fetch\s*\(|new WebSocket\s*\(|XMLHttpRequest)/);
    }
  });

  it('runs the actual build boundary guard against attempted native imports and networking', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'archive-boundary-test-'));
    const sourceDirectory = path.join(root, 'apps/desktop/src'); mkdirSync(sourceDirectory, { recursive: true });
    const fixture = path.join(sourceDirectory, 'attempt.ts');
    const checker = path.resolve('scripts/check-boundaries.mjs');
    try {
      for (const source of ["import fs from 'node:fs';", "import { ipcRenderer } from 'electron';", "fetch('https://example.invalid');"]) {
        writeFileSync(fixture, source);
        expect(() => execFileSync(process.execPath, [checker], { cwd: root, stdio: 'pipe' })).toThrow();
      }
      writeFileSync(fixture, "import type { Entry } from './contracts'; const label = 'opaque metadata';");
      expect(execFileSync(process.execPath, [checker], { cwd: root, encoding: 'utf8' })).toMatch(/Renderer native\/network.*boundar.*passed/);
    } finally { rmSync(root, { recursive: true, force: true }); }
  });


  it.each([
    { name: 'private cross-feature import', expected: 'cross-feature private import', files: { 'features/one/View.ts': "import { value } from '../two/private';", 'features/two/private.ts': 'export const value = 1;' } },
    { name: 'contract dependency inversion', expected: 'inverted contract dependency', files: { 'contracts/model.ts': "import { value } from '../app/state';", 'app/state.ts': 'export const value = 1;' } },
    { name: 'adapter dependency on UI', expected: 'adapter imports UI', files: { 'adapters/mock.ts': "import { value } from '../features/files/index';", 'features/files/index.ts': 'export const value = 1;' } },
    { name: 'dependency cycle', expected: 'dependency cycle', files: { 'first.ts': "import './second'; import { value } from './second'; export const first = 1;", 'second.ts': "import { first } from './first'; export const value = 1;" } },
  ])('rejects $name in the actual module boundary guard', ({ expected, files }) => {
    const root = mkdtempSync(path.join(tmpdir(), 'archive-module-guard-'));
    try {
      for (const [name, source] of Object.entries(files)) { const filename = path.join(root, 'apps/desktop/src', name); mkdirSync(path.dirname(filename), { recursive: true }); writeFileSync(filename, source!); }
      expect(() => execFileSync(process.execPath, [path.resolve('scripts/check-boundaries.mjs')], { cwd: root, stdio: 'pipe' })).toThrow(expected);
    } finally { rmSync(root, { recursive: true, force: true }); }
  });

  it('keeps restrictive production CSP and deny-by-default desktop source policy', () => {
    const html = readFileSync('apps/desktop/index.html', 'utf8');
    expect(html).toContain("default-src 'self'"); expect(html).toContain("script-src 'self'"); expect(html).toContain("connect-src 'none'"); expect(html).toContain("object-src 'none'");
    expect(html).not.toContain('unsafe-eval'); expect(html).not.toMatch(/script-src[^;]*unsafe-inline/);
    const main = readFileSync('apps/desktop/electron/main.cjs', 'utf8');
    for (const setting of ['sandbox:true', 'contextIsolation:true', 'nodeIntegration:false', 'webSecurity:true', "action:'deny'", 'setPermissionRequestHandler', 'setPermissionCheckHandler']) expect(main).toContain(setting);
  });
  it('executes the preload against a stub IPC to verify the bridge is fixed, frozen, and validates replies', async () => {
    const expose = vi.fn(); const invoke = vi.fn(async () => ({ schemaVersion: 1, appVersion: '0.1.0', mode: 'demo',configured:false }));
    runInNewContext(readFileSync('apps/desktop/electron/preload.cjs', 'utf8'), { require: () => ({ contextBridge: { exposeInMainWorld: expose }, ipcRenderer: { invoke,sendSync:()=>({schemaVersion:1,mode:'demo',configured:false}) } }) });
    expect(expose).toHaveBeenCalledTimes(1); expect(expose.mock.calls[0][0]).toBe('archiveDesktop');
    const bridge = expose.mock.calls[0][1] as { getVersion(): Promise<unknown>; backend:Record<string,(args:unknown)=>Promise<unknown>> };
    expect(Object.keys(bridge)).toEqual(['mode','backend','getVersion']);expect(Object.isFrozen(bridge.backend)).toBe(true);expect(Object.keys(bridge.backend)).not.toContain('invoke');await bridge.backend.listDrives({});expect(invoke).toHaveBeenCalledWith('archive:listDrives',{}); expect(Object.isFrozen(bridge)).toBe(true);
    await bridge.getVersion(); expect(invoke).toHaveBeenCalledWith('desktop:version');
    invoke.mockResolvedValueOnce({ schemaVersion: 99, appVersion: 'fake', mode: 'demo',configured:false });
    await expect(bridge.getVersion()).rejects.toThrow('Invalid desktop response');
  });
  it('executes main handlers against Electron stubs to reject unknown senders, URLs, and message arguments', async () => {
    const handlers = new Map<string, (...args: unknown[]) => unknown>();
    let webContents: object = {};const frame={url:'file:///demo/dist/index.html'};
    class FakeWindow {
      webContents = { mainFrame:frame,setWindowOpenHandler: vi.fn(), on: vi.fn() };
      constructor() { webContents = this.webContents; }
      loadFile = vi.fn(); isDestroyed = () => false;
      static getAllWindows() { return []; }
    }
    const electron = { app: { whenReady: () => Promise.resolve(), on: vi.fn(), getVersion: () => '0.1.0', quit: vi.fn() }, BrowserWindow: FakeWindow, ipcMain: { on:vi.fn(),handle: (name: string, fn: (...args: unknown[]) => unknown) => handlers.set(name, fn) }, session: { defaultSession: { setPermissionRequestHandler: vi.fn(), setPermissionCheckHandler: vi.fn() } } };
    const realRequire=createRequire(import.meta.url);
    const service=realRequire('../../electron/service-client.cjs');
    runInNewContext(readFileSync('apps/desktop/electron/main.cjs', 'utf8'), { URL,require: (name: string) => name === 'electron' ? electron : name==='node:path'?path:name==='node:url'?{pathToFileURL}:name==='./service-client.cjs'?{...service,readServiceConfig:()=>({mode:'service',configured:false})}:name==='./image-preview.cjs'?realRequire('../../electron/image-preview.cjs'):name==='./folder-owners.cjs'?realRequire('../../electron/folder-owners.cjs'):realRequire('../../electron/native-selections.cjs'), __dirname: '/demo/electron' });
    await Promise.resolve();
    expect([...handlers.keys()]).toContain('desktop:version');expect([...handlers.keys()]).toContain('archive:submitOperation');expect([...handlers.keys()]).not.toContain('archive:invoke');
    const handler = handlers.get('desktop:version')!;
    expect(handler({ sender: webContents, senderFrame: frame })).toMatchObject({ mode: 'service', schemaVersion: 1, configured:false });
    expect(() => handler({ sender: {}, senderFrame: frame })).toThrow();
    expect(() => handler({ sender: webContents, senderFrame: { url: 'https://example.invalid' } })).toThrow();
    expect(() => handler({ sender: webContents, senderFrame: frame }, { command: 'delete', path: '/' })).toThrow();
    frame.url='file:///demo/other.html';expect(()=>handler({sender:webContents,senderFrame:frame})).toThrow();frame.url='file:///demo/dist/index.html';
    await expect(handlers.get('archive:listDrives')!({sender:webContents,senderFrame:frame},{url:'https://evil.invalid'})).resolves.toMatchObject({ok:false,error:{code:'INVALID_REQUEST'}});
    await expect(handlers.get('archive:listDrives')!({sender:webContents,senderFrame:{url:frame.url}},{})).resolves.toMatchObject({ok:false,error:{code:'PERMISSION_DENIED'}});
  });
});
