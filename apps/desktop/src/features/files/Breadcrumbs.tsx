import {archiveNameLabel} from '../../shared/lib/archiveLabels';
import { ChevronRight, HardDrive } from 'lucide-react';
import type { Location } from '../../contracts';
interface Props { location: Location; label: string; onNavigate: (location: Location) => void }
export function Breadcrumbs({ location, label, onNavigate }: Props) {
  const parts = location.path.split('/').filter(Boolean);
  return <nav className="files-breadcrumbs" aria-label="Folder path">
    <button title={label} onClick={() => onNavigate({ driveId: location.driveId, path: '' })} aria-current={!parts.length ? 'location' : undefined}><HardDrive size={15}/><span>{label}</span></button>
    {parts.map((part, index) => <span className="files-crumb" key={`${index}-${part}`}><ChevronRight size={13} aria-hidden="true"/><button title={archiveNameLabel(part)} onClick={() => onNavigate({ driveId: location.driveId, path: parts.slice(0, index + 1).join('/') })} aria-current={index === parts.length - 1 ? 'location' : undefined}><bdi>{archiveNameLabel(part)}</bdi></button></span>)}
  </nav>;
}
