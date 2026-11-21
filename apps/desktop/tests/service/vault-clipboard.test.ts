import {createRequire} from 'node:module';
import {expect,it,vi} from 'vitest';
const require=createRequire(import.meta.url);
const create=require('../../electron/vault-clipboard.cjs');
it('clears only its current clipboard value and respects later copies',()=>{let value='';const timers:(()=>void)[]=[];const clipboard={writeText:(text:string)=>{value=text;},readText:()=>value,clear:vi.fn(()=>{value='';})};const copy=create(clipboard,(fn:()=>void)=>timers.push(fn));copy('first');value='unrelated';timers[0]();expect(value).toBe('unrelated');copy('second');copy('second');timers[1]();expect(value).toBe('second');timers[2]();expect(value).toBe('');});
