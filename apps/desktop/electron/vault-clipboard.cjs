'use strict';
module.exports=function createVaultClipboard(clipboard,schedule=setTimeout){let generation=0;return text=>{if(typeof text!=='string')throw new Error('Invalid secret');const current=++generation;clipboard.writeText(text);schedule(()=>{if(current===generation&&clipboard.readText()===text)clipboard.clear();},30000);};};
