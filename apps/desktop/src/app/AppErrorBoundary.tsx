import { Component, type ReactNode } from 'react';
/** Last-resort renderer recovery. Feature boundaries are owned by the shell. */
export class FeatureBoundary extends Component<{children:ReactNode;name:string;mode?:'mock'|'service'},{failed:boolean}> {
 state={failed:false};
 static getDerivedStateFromError(){return {failed:true};}
 render(){const demo=this.props.mode!=='service';return this.state.failed?<main role="alert" className="panel-fallback"><h1>The Archive needs to restart</h1><p>{demo?'The demo interface stopped unexpectedly. Restarting resets this synthetic session and its in-memory jobs.':'The desktop interface stopped unexpectedly. Restart to reconcile the independent service queue.'}</p><p>{demo?'No real storage or independent backend is connected. This does not establish future service survival after a renderer failure.':'The service runs independently. Accepted jobs must be checked after reconnecting; client transfers may be waiting for this desktop.'}</p><button className="btn btn--primary" onClick={()=>window.location.reload()}>{demo?'Restart demo interface':'Restart interface'}</button></main>:this.props.children;}
}
