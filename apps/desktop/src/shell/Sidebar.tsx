import type { ReactNode } from 'react';
import { ArrowLeftRight, FolderClosed, Network, Settings2, Disc3, Tv, KeyRound, BookOpen } from 'lucide-react';
import { FeatureBoundary } from './FeatureBoundary';

const routes = [
  { id: 'github', label: 'GitHub Backup', icon: FolderClosed, code: 'GH' },
  { id: 'files', label: 'Files', icon: FolderClosed, code: '01' },
  { id: 'skill-library', label: 'Skill Library', icon: BookOpen, code: 'SK' },
  { id: 'password-manager', label: 'Password Manager', icon: KeyRound, code: 'PW' },
  { id: 'transfers', label: 'Transfers', icon: ArrowLeftRight, code: '02' },
  { id: 'yoinks', label: 'Yoinks', icon: Disc3, code: 'VD' },
  { id: 'anime', label: 'Anime', icon: Tv, code: 'AN' },
  { id: 'spotifydl', label: 'SpotifyDL', icon: Disc3, code: '03' },
  { id: 'connections', label: 'Connections', icon: Network, code: '04' },
  { id: 'settings', label: 'Settings', icon: Settings2, code: '05' },
];

export function Sidebar({ route, onRoute, children }: { route: string; onRoute: (route: string) => void; children: ReactNode }) {
  return <aside className="app-sidebar" aria-label="Navigation and storage drives">
    <nav className="primary-nav" aria-label="Main navigation">
      {routes.map(({ id, label, icon: Icon, code }) => <button key={id} type="button"
        className={`nav-item ${route === id || (id === 'files' && route === 'trash') ? 'is-current' : ''}`}
        aria-current={route === id || (id === 'files' && route === 'trash') ? 'page' : undefined} onClick={() => onRoute(id)}>
        <Icon size={19} strokeWidth={1.7}/><span>{label}</span><span className="nav-code" aria-hidden="true">{code}</span>
      </button>)}
    </nav>
    <div className="sidebar-section-heading"><span className="eyebrow">Storage drives</span><span className="sidebar-section-code">02</span></div>
    <div className="sidebar-content"><FeatureBoundary name="Storage navigation">{children}</FeatureBoundary></div>
    <div className="sidebar-foot"><span className="status-dot"/><span>LOCAL INTERFACE</span><span className="sidebar-foot-code">01</span></div>
  </aside>;
}
