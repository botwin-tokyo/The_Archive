import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react';
import {afterEach,expect,it,vi} from 'vitest';
import type {BackendPorts} from '../../contracts';
import {GitBackupView} from './GitBackupView';
afterEach(cleanup);
it('shows coverage and opens the saved source view',async()=>{
 const status={configured:true,settings:{enabled:true,scope:'controlled',branchOverrides:{},retention:30,timezone:'UTC'},identity:'owner',run:null,repositories:[{id:'1',fullName:'owner/repo',selectedBranch:'custom',commit:'abc123',status:'complete',published:true,path:'GitHub/owner/repo--1/current',coverage:[]}],history:[],lastSuccess:null,nextRun:null,error:null,preview:null,previewing:false,coverageNotice:'Git data only'};
 const ports={gitBackup:{status:vi.fn().mockResolvedValue(status)}} as unknown as BackendPorts;
 const navigate=vi.fn();render(<GitBackupView ports={ports} onNavigate={navigate}/>);
 await screen.findByText('owner/repo');expect(screen.getByText('Git data only')).toBeTruthy();
 fireEvent.click(screen.getByText('Open files'));expect(navigate).toHaveBeenCalledWith({driveId:'git',path:'GitHub/owner/repo--1/current'});
 await waitFor(()=>expect(ports.gitBackup!.status).toHaveBeenCalled());
});
