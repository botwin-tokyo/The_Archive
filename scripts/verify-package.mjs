import fs from 'node:fs';
import path from 'node:path';
import {extractFile,listPackage} from '@electron/asar';
const arch=process.argv[2]??'arm64';
const directory=path.resolve(`release/TheArchive-linux-${arch}`);
const executable=fs.readFileSync(path.join(directory,'TheArchive'));
if(executable.subarray(0,4).toString('hex')!=='7f454c46')throw new Error('Not an ELF executable');
const machine=executable.readUInt16LE(18);if(machine!==(arch==='arm64'?183:62))throw new Error('Unexpected ELF architecture');
const archive=path.join(directory,'resources/app.asar');
const files=listPackage(archive);if(files.some(file=>!file.startsWith('/node_modules/zod')&&(/\/src\/|\/tests\//.test(file)||file.startsWith('/node_modules/')&&file!='/node_modules')))throw new Error('Unexpected source or unapproved dependency in runtime archive');
if(!files.includes('/node_modules/zod/package.json'))throw new Error('Missing pinned main-process schema validator');
const html=extractFile(archive,'dist/index.html').toString();if(!html.includes("connect-src 'none'")||html.includes("script-src 'self' 'unsafe-inline'"))throw new Error('Production CSP weakened');
const main=extractFile(archive,'electron/main.cjs').toString();for(const setting of ['sandbox:true','contextIsolation:true','nodeIntegration:false'])if(!main.includes(setting))throw new Error('Missing security setting '+setting);
for(const match of html.matchAll(/(?:src|href)="(\.\/assets\/[^"]+)"/g)){if(!files.includes('/dist/'+match[1].slice(2)))throw new Error('Missing offline asset '+match[1]);}
for(const file of files.filter(file=>file.endsWith('.js'))){const source=extractFile(archive,file.slice(1)).toString();if(source.includes('Developer demo scenarios'))throw new Error('Development scenario controls shipped in production');}
console.log(`Verified ${arch} ELF machine ${machine}; ${files.length} bundled paths; local assets; production CSP and sandbox flags. Native execution NOT verified.`);
