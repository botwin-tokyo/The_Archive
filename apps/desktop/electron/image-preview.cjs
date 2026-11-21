'use strict';
const {Buffer}=require('node:buffer');
const {failure}=require('./service-client.cjs');
const MAX_BYTES=16*1024*1024,MAX_PIXELS=32000000,MAX_SIDE=16384,CHUNK=1024*1024;
function dimensions(data){
 let mime,width,height;
 if(data.length>=24&&data.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))&&data.toString('ascii',12,16)==='IHDR'){
  mime='image/png';width=data.readUInt32BE(16);height=data.readUInt32BE(20);
 }else if(data.length>=10&&['GIF87a','GIF89a'].includes(data.toString('ascii',0,6))){
  mime='image/gif';width=data.readUInt16LE(6);height=data.readUInt16LE(8);
 }else if(data.length>=30&&data.toString('ascii',0,4)==='RIFF'&&data.toString('ascii',8,12)==='WEBP'){
  mime='image/webp';const kind=data.toString('ascii',12,16);
  if(kind==='VP8X'){width=1+data.readUIntLE(24,3);height=1+data.readUIntLE(27,3);}
  else if(kind==='VP8 '&&data.subarray(23,26).equals(Buffer.from([157,1,42]))){width=data.readUInt16LE(26)&0x3fff;height=data.readUInt16LE(28)&0x3fff;}
  else if(kind==='VP8L'&&data[20]===47){const bits=data.readUInt32LE(21);width=(bits&0x3fff)+1;height=((bits>>>14)&0x3fff)+1;}
 }else if(data.length>=4&&data[0]===255&&data[1]===216){
  mime='image/jpeg';let offset=2;
  while(offset+4<=data.length){
   if(data[offset++]!==255)break;
   while(data[offset]===255)offset++;
   const marker=data[offset++];if(marker===218||marker===217)break;
   if(marker===1||(marker>=208&&marker<=215))continue;
   if(offset+2>data.length)break;
   const length=data.readUInt16BE(offset);if(length<2||offset+length>data.length)break;
   if([192,193,194,195,197,198,199,201,202,203,205,206,207].includes(marker)&&length>=8){height=data.readUInt16BE(offset+3);width=data.readUInt16BE(offset+5);break;}
   offset+=length;
  }
 }
 if(!mime||!width||!height)throw failure('UNSUPPORTED','This file is not a supported PNG, JPEG, GIF, or WebP image.');
 if(width>MAX_SIDE||height>MAX_SIDE||width*height>MAX_PIXELS)throw failure('UNSUPPORTED','This image is too large to preview (maximum 32 megapixels and 16,384 pixels per side).');
 return {mime,width,height};
}
function createImagePreview(client){
 let generation=0;
 return async ref=>{
  const request=++generation;
  const current=()=>{if(request!==generation)throw failure('ABORTED','A newer image preview was selected');};
  const entry=await client.call('files.stat',{ref});current();
  if(entry.kind!=='file'||!entry.capabilities.read)throw failure('UNSUPPORTED','Only readable image files can be previewed.');
  if(!/^(0|[1-9][0-9]*)$/.test(entry.sizeBytes??''))throw failure('INVALID_RESPONSE','Image size could not be validated');
  const size=BigInt(entry.sizeBytes);
  if(size===0n||size>BigInt(MAX_BYTES))throw failure('UNSUPPORTED','Image previews support files up to 16 MiB. Download this file to view it.');
  const chunks=[];let header;
  for(let offset=0;offset<Number(size);offset+=CHUNK){
   current();const length=Math.min(CHUNK,Number(size)-offset);
   const part=await client.download(ref,String(offset),length);current();
   if(String(part.revision)!==String(ref.revision)||part.bytes.length!==length)throw failure('SOURCE_CHANGED','The image changed while its preview was loading. Refresh the folder and try again.');
   chunks.push(part.bytes);
   // Reject unsupported files and oversized canvases before reading the rest.
   if(offset===0)header=dimensions(part.bytes);
  }
  await client.call('files.stat',{ref});current();
  return {...header,dataUrl:`data:${header.mime};base64,${Buffer.concat(chunks).toString('base64')}`,sizeBytes:String(size)};
 };
}
module.exports={createImagePreview,dimensions,MAX_BYTES};
