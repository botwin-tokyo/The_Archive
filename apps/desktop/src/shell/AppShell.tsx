import type {ServiceStatus} from '../contracts';
import { Suspense, useState, type CSSProperties, type ReactNode } from 'react';
import { ChevronDown, ChevronUp, HardDrive, PanelRightClose, PanelRightOpen, X } from 'lucide-react';
import { Dialog, IconButton, Splitter } from '../shared/ui';
import { Sidebar } from './Sidebar';
import { StatusBar } from './StatusBar';
import { FeatureBoundary } from './FeatureBoundary';
import { useMediaQuery, usePresentationNumber } from './presentation';

export interface AppShellProps {
  route: string;
  mode?: 'mock'|'service';
  status?:ServiceStatus|null;
  onRoute: (route: string) => void;
  sidebar: ReactNode;
  children: ReactNode;
  inspector: ReactNode;
  queue: ReactNode;
}

export function AppShell({ route, onRoute, sidebar, children, inspector, queue,status,mode='mock' }: AppShellProps) {
  const compactInspector = useMediaQuery('(max-width: 1279px)');
  const [inspectorOpen, setInspectorOpen] = usePresentationNumber('inspector-open', 1, 0, 1);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = usePresentationNumber('inspector-width', 310, 280, 400);
  const [queueHeight, setQueueHeight] = usePresentationNumber('queue-height', 250, 170, 420);
  const [queueCollapsed, setQueueCollapsed] = usePresentationNumber('queue-collapsed', 0, 0, 1);
  const hasInspector = Boolean(inspector) && ['files', 'trash'].includes(route);
  const hasQueue = Boolean(queue) && !['transfers','password-manager','skill-library'].includes(route);
  const desktopInspector = hasInspector && !compactInspector && Boolean(inspectorOpen);
  const style = { '--inspector-width': `${inspectorWidth}px`, '--queue-height': `${queueHeight}px` } as CSSProperties;
  const toggleInspector = () => compactInspector ? setDrawerOpen(!drawerOpen) : setInspectorOpen(inspectorOpen ? 0 : 1);

  return <div className="app-shell" style={style}>
    <header className="app-header">
      <div className="brand"><div className="brand-mark"><HardDrive size={25} strokeWidth={1.65}/></div><h1>THE ARCHIVE</h1></div>
      <div className="header-stripes" aria-hidden="true"/>
      <div className="header-descriptor"><span>INDEPENDENT STORAGE SYSTEM</span><span className="header-subtitle">Local control. Clear boundaries.</span></div>
      <div className="header-right"><span className="demo-label"><span className="status-dot"/>{mode==='mock'?'DEMO DATA':'SERVICE MODE'}</span>
        <span className="header-version">{mode==='mock'?'FRONTEND PROTOTYPE':'LOCAL SERVICE CLIENT'}</span>
        {hasInspector && <IconButton label={compactInspector ? 'Open selection inspector' : inspectorOpen ? 'Hide inspector' : 'Show inspector'} onClick={toggleInspector}
          aria-expanded={compactInspector ? drawerOpen : Boolean(inspectorOpen)} aria-controls={compactInspector ? undefined : 'selection-inspector'}>
          {desktopInspector ? <PanelRightClose size={19}/> : <PanelRightOpen size={19}/>}
        </IconButton>}
      </div>
    </header>
    <div className="app-body">
      <Sidebar route={route} onRoute={onRoute}>{sidebar}</Sidebar>
      <div className={`workspace ${hasQueue ? 'workspace--with-queue' : ''} ${queueCollapsed ? 'workspace--queue-collapsed' : ''}`}>
        <div className="workspace-top">
          <main className="workspace-main" id="main-content" aria-label={route === 'files' ? 'Files browser' : route}>
            <FeatureBoundary name={route === 'files' ? 'Files browser' : route} resetKey={route}>
              <Suspense fallback={<div className="panel-loading" role="status"><span className="loading-mark"/>Loading {route}…</div>}>{children}</Suspense>
            </FeatureBoundary>
          </main>
          {desktopInspector && <>
            <Splitter orientation="vertical" value={inspectorWidth} min={280} max={400} onChange={setInspectorWidth} reverse label="Resize inspector" controls="selection-inspector"/>
            <aside className="workspace-inspector" id="selection-inspector" aria-label="Selection inspector">
              <div className="inspector-chrome"><span className="eyebrow">Inspector</span><IconButton label="Hide inspector" onClick={() => setInspectorOpen(0)}><X size={15}/></IconButton></div>
              <div className="shell-inspector-content"><FeatureBoundary name="Inspector">{inspector}</FeatureBoundary></div>
            </aside>
          </>}
        </div>
        {hasQueue && <>
          <div className="queue-seam">
            {!queueCollapsed && <Splitter orientation="horizontal" value={queueHeight} min={170} max={420} onChange={setQueueHeight} reverse label="Resize transfer queue" controls="transfer-queue"/>}
            <button className="queue-collapse" type="button" aria-label={queueCollapsed ? 'Expand transfer queue' : 'Collapse transfer queue'} aria-expanded={!queueCollapsed}
              aria-controls="transfer-queue" title={queueCollapsed ? 'Expand transfer queue' : 'Collapse transfer queue'} onClick={() => setQueueCollapsed(queueCollapsed ? 0 : 1)}>
              {queueCollapsed ? <ChevronUp size={12}/> : <ChevronDown size={12}/>}</button>
          </div>
          <section id="transfer-queue" className={`workspace-queue ${queueCollapsed ? 'is-collapsed' : ''}`} aria-label="Transfer queue panel">
            <div className="queue-content" hidden={Boolean(queueCollapsed)}><FeatureBoundary name="Transfer queue">{queue}</FeatureBoundary></div>
            {Boolean(queueCollapsed) && <button className="queue-summary" type="button" onClick={() => setQueueCollapsed(0)}><span className="eyebrow">Transfer queue</span><span>Show transfer activity</span><ChevronUp size={16}/></button>}
          </section>
        </>}
      </div>
    </div>
    <StatusBar mode={mode} onRoute={onRoute} status={status}/>
    <Dialog open={hasInspector && compactInspector && drawerOpen} onClose={() => setDrawerOpen(false)} title="Selection inspector" className="inspector-drawer">
      <FeatureBoundary name="Inspector">{inspector}</FeatureBoundary>
    </Dialog>
  </div>;
}
