import React from 'react';
import {afterEach,it,expect,vi} from 'vitest';
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {TrashView} from '../../src/features/trash';
import {createMockBackend} from '../../src/adapters/mock';
import {refOf} from '../../src/contracts';
import {INITIAL_LOCATION} from '../../src/fixtures/drives';
afterEach(cleanup);
it('shows only the selected drive Trash and forwards exact purge targets',async()=>{const backend=createMockBackend({seedJobs:false});const drives=await backend.drives.list();const entry=(await backend.files.list({location:INITIAL_LOCATION,requestId:'list'})).entries.find(entry=>entry.kind==='file')!;await backend.operations.submit({requestId:'trash-test',kind:'trash',targets:[refOf(entry)]});backend.clock.advance(10000);const action=vi.fn(),onDrive=vi.fn();render(<TrashView port={backend.trash} drives={drives} drive={drives[0]} onDrive={onDrive} onOperation={action} refreshKey={0}/>);await waitFor(()=>expect(screen.queryByText('Reading Trash…')).toBeNull());fireEvent.click(screen.getByRole('checkbox',{name:`Select ${entry.name}`}));fireEvent.click(screen.getByRole('button',{name:'Permanently delete selected'}));expect(action.mock.calls[0][0]).toBe('purge');expect(action.mock.calls[0][1][0].id).toBe(entry.id);fireEvent.change(screen.getByRole('combobox',{name:'Trash drive'}),{target:{value:drives[1].id}});expect(onDrive).toHaveBeenCalledWith(drives[1].id);});
