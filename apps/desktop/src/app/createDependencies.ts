import {createMockBackend} from '../adapters/mock';
import {createServiceBackend} from '../adapters/service/createServiceBackend';
/** Only explicit demo selection uses fixtures. Production defaults to unconfigured service. */
export function selectedMode():'mock'|'service'{
 const bridge=window.archiveDesktop;
 const demo=bridge?bridge.mode==='demo':import.meta.env.MODE==='test'||new URLSearchParams(window.location.search).get('mode')==='demo';
 return demo?'mock':'service';
}

export function createDependencies(){return selectedMode()==='mock'?createMockBackend():createServiceBackend(window.archiveDesktop);}
