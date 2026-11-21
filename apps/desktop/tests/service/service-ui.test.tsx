import {afterEach,expect,it} from 'vitest';
import {cleanup,render,screen} from '@testing-library/react';
import App from '../../src/app/App';
import {SERVICE_ACTIONS,type DesktopBridge} from '../../src/adapters/service/createServiceBackend';
import {DiagnosticsView} from '../../src/features/diagnostics';
import {validateSettingsDraft} from '../../src/features/settings/draft';
import {createMockBackend} from '../../src/adapters/mock';
afterEach(()=>{cleanup();delete window.archiveDesktop;});
it('shows unconfigured service mode without demo identities, metrics, or simulated queue labels',async()=>{
 window.archiveDesktop={mode:'service',getVersion:async()=>({schemaVersion:1,appVersion:'fixture',mode:'service',configured:false}),backend:Object.fromEntries(SERVICE_ACTIONS.map(action=>[action,async(_args:unknown)=>({version:1,mode:'service',ok:false,error:{code:'UNAVAILABLE',message:'Service is not configured',retryable:false,correlationId:'fixture',recovery:[]}})])) as DesktopBridge['backend']};
 render(<App/>);expect(await screen.findByText('SERVICE MODE')).toBeTruthy();expect(await screen.findByText('Service unavailable or unconfigured')).toBeTruthy();expect(screen.queryByText('DEMO DATA')).toBeNull();expect(screen.queryByText('SIMULATED')).toBeNull();expect(screen.queryByText('Developer demo scenarios')).toBeNull();expect(screen.getByRole('button',{name:'Upload'})).toHaveProperty('disabled',true);
});
it('renders allowlisted real diagnostics without raw messages or false simulated wording',async()=>{
 render(<DiagnosticsView simulated={false} port={{list:async()=>[{id:'1',at:'2026-09-30T09:00:00.123456Z',level:'warning',code:'UNAVAILABLE',message:'SECRET_TOKEN /private/path',correlationId:'a'.repeat(32),simulated:false}]}}/>);expect(await screen.findByText('A service is unavailable.')).toBeTruthy();expect(screen.queryByText(/SECRET_TOKEN/)).toBeNull();expect(screen.queryByText('Time unknown')).toBeNull();expect(screen.queryByText('Reading synthetic diagnostics…')).toBeNull();
});
it('allows secure production health endpoints while retaining the reserved demo-host restriction',async()=>{const mock=createMockBackend();try{const config=await mock.configuration.get();const health={enabled:true,endpoint:'wss://health.example.com/metrics',intervalSeconds:30};expect(validateSettingsDraft({...config,health})['health.endpoint']).toMatch(/demo/);expect(validateSettingsDraft({...config,health,simulated:false})).toEqual({});}finally{mock.dispose();}});
