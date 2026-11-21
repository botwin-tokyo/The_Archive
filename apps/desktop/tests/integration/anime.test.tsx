import React from 'react';
import {afterEach,expect,it,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,within} from '@testing-library/react';
import {AnimeView} from '../../src/features/anime';
import {createMockBackend} from '../../src/adapters/mock/createMockBackend';
import type {AnimeSeries} from '../../src/contracts';
afterEach(()=>{cleanup();vi.restoreAllMocks();localStorage.clear();});

async function openSeries(name=/^Archive Lights, /){
 const ports=createMockBackend();const view=render(<AnimeView ports={ports} onNavigate={()=>{}}/>);
 await screen.findByText('Ready to download');
 fireEvent.change(screen.getByRole('searchbox',{name:'Search anime'}),{target:{value:'Archive'}});fireEvent.submit(screen.getByRole('search'));
 fireEvent.click(await screen.findByRole('button',{name}));
 await screen.findByRole('heading',{level:2,name:/Archive Lights/});
 return {ports,view};
}

it('searches, reviews an exact selection and queues it with one stable request',async()=>{
 const {ports}=await openSeries();const enqueue=vi.spyOn(ports.anime,'enqueue');const resolve=vi.spyOn(ports.anime,'resolve');
 expect(screen.getByText('Matched by AniList ID')).toBeTruthy();
 fireEvent.change(screen.getByRole('textbox',{name:'Range start episode'}),{target:{value:'1'}});fireEvent.change(screen.getByRole('textbox',{name:'Range end episode'}),{target:{value:'3'}});
 fireEvent.click(screen.getByRole('button',{name:'Add range'}));
 expect(screen.getByText(/3 of 13 available sub episodes selected/)).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'Review 3 episodes'}));
 const dialog=await screen.findByRole('dialog',{name:'Review Archive Lights'});
 expect(resolve.mock.calls[0][0]).toMatchObject({audio:'sub',quality:'1080p',subtitles:['en'],episodeIds:['demo.1.1','demo.1.2','demo.1.3']});
 expect(within(dialog).getByText(/2 to download · 0 already present · 1 conflict · 0 unavailable/)).toBeTruthy();
 expect(within(dialog).getByText('Episode 003 [sub].mp4')).toBeTruthy();expect(within(dialog).getByText(/Name conflict/)).toBeTruthy();
 fireEvent.click(within(dialog).getByRole('button',{name:'Queue 2 episodes'}));
 await screen.findByText(/Queued 2 episodes of Archive Lights/);
 expect(enqueue).toHaveBeenCalledTimes(1);
 const queue=screen.getByRole('complementary',{name:'Anime downloads'});await within(queue).findByText('Archive Lights');
 fireEvent.click(within(queue).getByRole('button',{name:/Archive Lights/}));expect(within(queue).getAllByText('Demo only — no files are created')).toHaveLength(2);
 ports.dispose();
});

it('reconciles an uncertain enqueue with the same request ID instead of submitting again',async()=>{
 const {ports}=await openSeries();const original=ports.anime.enqueue;
 const enqueue=vi.spyOn(ports.anime,'enqueue').mockImplementationOnce(async args=>{await original(args);throw new Error('Connection lost');});
 const reconcile=vi.spyOn(ports.anime,'reconcile');
 fireEvent.click(screen.getByRole('button',{name:'Select all 13 available'}));fireEvent.click(screen.getByRole('button',{name:'Review 13 episodes'}));
 fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button',{name:/Queue 12 episodes/}));
 await screen.findByText(/Queued 12 episodes/);
 expect(enqueue).toHaveBeenCalledTimes(1);expect(reconcile).toHaveBeenCalledWith(enqueue.mock.calls[0][0].requestId);
 ports.dispose();
});

it('keeps an opened series when the typing debounce fires after an explicit search',async()=>{
 vi.useFakeTimers({shouldAdvanceTime:true});
 try{
  const {ports}=await openSeries();const search=vi.spyOn(ports.anime,'search');
  await vi.advanceTimersByTimeAsync(1000);
  expect(screen.getByRole('heading',{level:2,name:'Archive Lights'})).toBeTruthy();expect(search).not.toHaveBeenCalled();
  ports.dispose();
 }finally{vi.useRealTimers();}
});

it('discards an obsolete search response that arrives after a newer one',async()=>{
 const ports=createMockBackend();const original=ports.anime.search;let release:()=>void=()=>{};
 vi.spyOn(ports.anime,'search').mockImplementationOnce(async args=>{await new Promise<void>(r=>{release=r;});return {...await original(args),items:[{anilistId:1,malId:null,title:'Obsolete Result',altTitles:[],year:2000,format:'TV',status:null,episodes:1,hasCover:false}]};}).mockImplementation(original);
 render(<AnimeView ports={ports} onNavigate={()=>{}}/>);await screen.findByText('Ready to download');
 const box=screen.getByRole('searchbox',{name:'Search anime'});
 fireEvent.change(box,{target:{value:'old'}});fireEvent.submit(screen.getByRole('search'));
 fireEvent.change(box,{target:{value:'Archive'}});fireEvent.submit(screen.getByRole('search'));
 await screen.findByRole('button',{name:/^Archive Lights, /});release();await new Promise(r=>setTimeout(r,20));
 expect(screen.queryByText('Obsolete Result')).toBeNull();
 ports.dispose();
});

it('requires an explicit provider choice for an ambiguous series',async()=>{
 const {ports}=await openSeries(/Second Light, /);const lookup=vi.spyOn(ports.anime,'lookup');
 expect(screen.getByText(/Several provider series could match/)).toBeTruthy();expect(screen.queryByRole('button',{name:/^Review/})).toBeNull();
 const candidates=screen.getByRole('list',{name:'Provider series candidates'});
 fireEvent.click(within(candidates).getAllByRole('button',{name:'Use this series'})[1]);
 await screen.findByText('Using your selected provider series');
 expect(lookup.mock.calls[0][0]).toMatchObject({anilistId:900002,providerSeriesId:'demo/archive-lights-second-light-2'});
 ports.dispose();
});

it('deselects episodes without the chosen audio and explains why',async()=>{
 const {ports}=await openSeries();
 fireEvent.click(screen.getByRole('button',{name:'Select all 13 available'}));
 fireEvent.click(screen.getByRole('button',{name:'English · dub'}));
 expect(await screen.findByText(/2 selected episodes have no dub release and were deselected/)).toBeTruthy();
 expect(screen.getByText(/11 of 11 available dub episodes selected/)).toBeTruthy();
 expect((screen.getByRole('checkbox',{name:/^Episode 7 \(no dub release\)/}) as HTMLInputElement).disabled).toBe(true);
 ports.dispose();
});

it('caps a batch at 200 episodes and pages long inventories without implying selection',async()=>{
 const ports=createMockBackend();const original=ports.anime.lookup;
 vi.spyOn(ports.anime,'lookup').mockImplementation(async args=>{const value=await original(args);const episodes=Array.from({length:250},(_,i)=>({id:`long.${i+1}`,number:String(i+1),title:null,sub:true,dub:false}));return {...value,episodes} as AnimeSeries;});
 render(<AnimeView ports={ports} onNavigate={()=>{}}/>);await screen.findByText('Ready to download');
 fireEvent.change(screen.getByRole('searchbox',{name:'Search anime'}),{target:{value:'Archive'}});fireEvent.submit(screen.getByRole('search'));
 fireEvent.click(await screen.findByRole('button',{name:/^Archive Lights, /}));await screen.findByRole('heading',{level:2,name:'Archive Lights'});
 expect(screen.getAllByRole('checkbox')).toHaveLength(100);expect(screen.getByRole('button',{name:'Show 100 more of 150 remaining'})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'Select all 250 available'}));
 expect(screen.getByRole('alert').textContent).toMatch(/at most 200 episodes/);expect((screen.getByRole('button',{name:'Review 250 episodes'}) as HTMLButtonElement).disabled).toBe(true);
 ports.dispose();
});

it('moves focus to the series heading and restores focus after closing review with Escape',async()=>{
 const {ports}=await openSeries();
 expect(document.activeElement).toBe(screen.getByRole('heading',{level:2,name:'Archive Lights'}));
 fireEvent.click(screen.getByRole('checkbox',{name:'Episode 1: First Light'}));const review=screen.getByRole('button',{name:'Review 1 episode'});review.focus();fireEvent.click(review);
 await screen.findByRole('dialog');fireEvent.keyDown(document,{key:'Escape'});
 await vi.waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());expect(document.activeElement).toBe(review);
 ports.dispose();
});

it('shows lookup failures with retry and never cancels jobs when the page closes',async()=>{
 const ports=createMockBackend();const control=vi.spyOn(ports.anime,'control');
 vi.spyOn(ports.anime,'lookup').mockResolvedValueOnce({id:'failed',revision:1,state:'failed',anilistId:900001,requestedProvider:null,catalog:null,description:null,genres:[],relations:[],candidates:[],match:{state:'pending',providerSeriesId:null,evidence:null},episodes:[],samples:{sub:null,dub:null},expiresAt:0,error:'Anime metadata is unavailable right now. Try again shortly.'});
 const view=render(<AnimeView ports={ports} onNavigate={()=>{}}/>);await screen.findByText('Ready to download');
 fireEvent.change(screen.getByRole('searchbox',{name:'Search anime'}),{target:{value:'Archive'}});fireEvent.submit(screen.getByRole('search'));
 fireEvent.click(await screen.findByRole('button',{name:/^Archive Lights, /}));
 expect((await screen.findByRole('alert')).textContent).toMatch(/metadata is unavailable/);
 fireEvent.click(screen.getByRole('button',{name:'Try again'}));await screen.findByRole('heading',{level:2,name:'Archive Lights'});
 view.unmount();expect(control).not.toHaveBeenCalled();
 ports.dispose();
});

it('names the destination drive and lets the folder picker switch writable storage drives',async()=>{
 const ports=createMockBackend();const drives=(await ports.drives.list()).filter(d=>d.role==='files'&&d.capabilities.write);const status=await ports.anime.status();
 render(<AnimeView ports={ports} onNavigate={()=>{}}/>);await screen.findByText('Ready to download');
 const label=drives.find(d=>d.id===status.defaultDestination!.driveId)!.label;expect(await screen.findByRole('button',{name:new RegExp(`^${label}`)})).toBeTruthy();
 fireEvent.click(screen.getByRole('button',{name:'Change folder'}));const dialog=await screen.findByRole('dialog',{name:'Choose a destination folder'});
 expect(within(dialog).getByRole('button',{name:label})).toBeTruthy();
 if(drives.length>1)expect(within(dialog).getByRole('group',{name:'Drive'})).toBeTruthy();
 ports.dispose();
});
