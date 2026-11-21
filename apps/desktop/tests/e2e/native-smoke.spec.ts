import {test,expect,_electron as electron} from '@playwright/test';
import path from 'node:path';
test('production Electron window runs offline with isolated renderer',async()=>{
 const env={...process.env,ARCHIVE_CONSOLE_MODE:'service',ARCHIVE_SERVICE_ENDPOINT:'',ARCHIVE_SERVICE_TOKEN:'',ARCHIVE_SERVICE_TOKEN_FILE:''};
 const app=await electron.launch(process.env.ARCHIVE_PACKAGED_BINARY?{executablePath:process.env.ARCHIVE_PACKAGED_BINARY,env}:{args:[path.resolve('.')],env});
 try{const window=await app.firstWindow();await expect(window.getByRole('heading',{name:'THE ARCHIVE',exact:true})).toBeVisible();await expect(window.getByText('Service unavailable or unconfigured',{exact:true})).toBeVisible();expect(await window.evaluate(()=>typeof (globalThis as unknown as {require?:unknown}).require)).toBe('undefined');expect(await window.evaluate(()=>typeof (globalThis as unknown as {process?:unknown}).process)).toBe('undefined');const bridge=await window.evaluate(async()=>{const desktop=(window as unknown as {archiveDesktop:{getVersion:()=>Promise<{mode:string}>}}).archiveDesktop;return desktop.getVersion();});expect(bridge.mode).toBe('service');expect(window.url().startsWith('file://')).toBe(true);
 for(const [width,height] of [[1600,1000],[1280,800],[1100,700],[900,600]]){await app.evaluate(({BrowserWindow},size)=>BrowserWindow.getAllWindows()[0].setContentSize(size[0],size[1]),[width,height]);await window.screenshot({path:`test-results/native/${width}x${height}.png`});await expect(window.getByRole('button',{name:'Files',exact:true})).toBeVisible();}
 }finally{await app.close();}
});

test('selecting a folder does not move the double-click target',async()=>{
 const {mkdtemp,rm}=await import('node:fs/promises');
 const {tmpdir}=await import('node:os');
 const profile=await mkdtemp(path.join(tmpdir(),'archive-folder-navigation-'));
 const env={...process.env,ARCHIVE_CONSOLE_MODE:'demo',ARCHIVE_SERVICE_ENDPOINT:'',ARCHIVE_SERVICE_TOKEN:'',ARCHIVE_SERVICE_TOKEN_FILE:''};
 const args=[`--user-data-dir=${profile}`];
 const app=await electron.launch(process.env.ARCHIVE_PACKAGED_BINARY?{executablePath:process.env.ARCHIVE_PACKAGED_BINARY,args,env}:{args:[path.resolve('.'),...args],env});
 try{
  const window=await app.firstWindow();
  const row=window.locator('.files-table tbody tr').filter({has:window.getByText('Photography',{exact:true})});
  await expect(row).toBeVisible();
  const before=(await row.boundingBox())!;
  const x=before.x+120,y=before.y+before.height/2;
  await window.mouse.click(x,y);
  await expect(row).toHaveAttribute('aria-selected','true');
  const selected=(await row.boundingBox())!;
  expect(selected.y).toBe(before.y);expect(selected.height).toBe(before.height);
  await window.mouse.click(x,y,{clickCount:2});
  await expect(window.locator('.files-breadcrumbs [aria-current="location"]')).toHaveText('Photography');
  await expect(window.getByText('Frame 001.dng',{exact:true})).toBeVisible();
 }finally{await app.close();await rm(profile,{recursive:true,force:true});}
});

test('selected images decode in the sandboxed viewer and zoom without opening another window',async()=>{
 const {mkdtemp,rm}=await import('node:fs/promises');const {tmpdir}=await import('node:os');
 const profile=await mkdtemp(path.join(tmpdir(),'archive-image-preview-'));
 const env={...process.env,ARCHIVE_CONSOLE_MODE:'demo',ARCHIVE_SERVICE_ENDPOINT:'',ARCHIVE_SERVICE_TOKEN:'',ARCHIVE_SERVICE_TOKEN_FILE:''};
 const args=[`--user-data-dir=${profile}`];
 const app=await electron.launch(process.env.ARCHIVE_PACKAGED_BINARY?{executablePath:process.env.ARCHIVE_PACKAGED_BINARY,args,env}:{args:[path.resolve('.'),...args],env});
 try{
  const window=await app.firstWindow();
  await window.locator('.files-table tbody tr').filter({has:window.getByText('Console study.png',{exact:true})}).click();
  const thumbnail=window.locator('.image-preview-thumbnail img');
  await expect(thumbnail).toBeVisible();
  await expect.poll(()=>thumbnail.evaluate((image:HTMLImageElement)=>image.naturalWidth)).toBe(96);
  await window.getByRole('button',{name:'Open image preview: Console study.png'}).click();
  const dialog=window.getByRole('dialog',{name:'Console study.png'});await expect(dialog).toBeVisible();
  expect((await dialog.boundingBox())!.width).toBeGreaterThan(480);
  await dialog.getByRole('button',{name:'Actual size'}).click();await expect(dialog.getByText('100%',{exact:true})).toBeVisible();
  await dialog.getByRole('button',{name:'Zoom in'}).click();await expect(dialog.getByText('150%',{exact:true})).toBeVisible();
  expect(app.windows()).toHaveLength(1);
  await window.keyboard.press('Escape');await expect(dialog).toHaveCount(0);
  await expect(window.getByRole('button',{name:'Open image preview: Console study.png'})).toBeFocused();
 }finally{await app.close();await rm(profile,{recursive:true,force:true});}
});

test('anime search, review and verified publication work through the real preload and service',async()=>{
 const {spawn}=await import('node:child_process');const {mkdtemp,rm}=await import('node:fs/promises');const {tmpdir}=await import('node:os');
 const fixture=spawn('python',['apps/desktop/tests/service/anime_fixture_service.py'],{env:{...process.env,PYTHONPATH:path.resolve('apps/backend/src')},stdio:'pipe'});
 const port=await new Promise<number>((resolve,reject)=>{let out='';const timer=setTimeout(()=>reject(new Error('Anime fixture startup timed out')),15000);fixture.stdout.on('data',c=>{out+=c;if(out.includes('\n')){clearTimeout(timer);resolve(JSON.parse(out.split('\n')[0]).port);}});fixture.once('exit',()=>reject(new Error('Anime fixture exited')));});
 const profile=await mkdtemp(path.join(tmpdir(),'archive-anime-native-'));
 const env={...process.env,ARCHIVE_CONSOLE_MODE:'service',ARCHIVE_SERVICE_ENDPOINT:`http://127.0.0.1:${port}`,ARCHIVE_SERVICE_TOKEN:'desktop-test-fixture-owner-credential-00000000',ARCHIVE_SERVICE_TOKEN_FILE:''};
 const args=[`--user-data-dir=${profile}`];
 const app=await electron.launch(process.env.ARCHIVE_PACKAGED_BINARY?{executablePath:process.env.ARCHIVE_PACKAGED_BINARY,args,env}:{args:[path.resolve('.'),...args],env});
 try{
  const window=await app.firstWindow();
  await window.getByRole('button',{name:'Anime'}).click();await expect(window.getByText('Ready to download')).toBeVisible({timeout:20000});
  expect(await window.evaluate(()=>Object.keys((window as unknown as {archiveDesktop:{backend:object}}).archiveDesktop.backend).filter(k=>/Anime/.test(k)).length)).toBe(13);
  await window.getByRole('searchbox',{name:'Search anime'}).fill('Frieren');await window.keyboard.press('Enter');
  await window.getByRole('button',{name:/^Fixture Frieren, /}).click();await expect(window.getByRole('heading',{level:2,name:'Fixture Frieren'})).toBeFocused();
  await window.getByRole('button',{name:'Select all 2 available'}).click();await window.getByRole('button',{name:'Review 2 episodes'}).click();
  await window.getByRole('dialog').getByRole('button',{name:'Use this folder'}).click();
  const review=window.getByRole('dialog',{name:'Review Fixture Frieren'});await expect(review.getByText('Episode 001 [sub].mp4')).toBeVisible();
  await review.getByRole('button',{name:'Queue 2 episodes'}).click();
  const queue=window.getByRole('complementary',{name:'Anime downloads'});await expect(queue.getByText('Complete')).toBeVisible({timeout:20000});
  for(const [width,height] of [[1600,1000],[1100,700],[900,600]]){await app.evaluate(({BrowserWindow},size)=>BrowserWindow.getAllWindows()[0].setContentSize(size[0],size[1]),[width,height]);
   expect(await window.evaluate(()=>{const el=document.querySelector('.anime')!;return el.scrollWidth-el.clientWidth;})).toBe(0);}
  await queue.getByRole('button',{name:/Open folder/}).click();
  const browser=window.getByRole('main',{name:'Files browser'});await expect(browser.getByText('Episode 001 [sub].mp4',{exact:true})).toBeVisible();await expect(browser.getByText('Episode 002 [sub].mp4',{exact:true})).toBeVisible();
 }finally{await app.close();await rm(profile,{recursive:true,force:true});fixture.stdin.end('\n');}
});
