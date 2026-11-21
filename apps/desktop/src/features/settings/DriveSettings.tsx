import { HardDrive, Archive } from 'lucide-react';
import type { Configuration, Drive } from '../../contracts';
import { bytes } from '../../shared/lib/format';
import type { SettingsErrors } from './draft';
interface Props { draft: Configuration; drives: Drive[]; driveError: boolean; lastHarnessTransfer: string | null; errors: SettingsErrors; onLabel: (id: string, label: string) => void; }
export function DriveSettings({ draft, drives, driveError, lastHarnessTransfer, errors, onLabel }: Props) {
  return <section className="settings-section" aria-labelledby="drive-settings-title"><div className="settings-section-heading"><HardDrive size={19}/><div><h3 id="drive-settings-title">Drives</h3><p>Independent volumes. Stable identities stay with their assigned purpose.</p></div></div>
    {driveError && <p className="settings-notice">Drive metadata is unavailable. Labels remain editable; hardware metrics are unknown.</p>}
    <div className="settings-drives">{Object.entries(draft.driveLabels).map(([id, label]) => {
      const drive = drives.find(item => item.id === id);
      return <div className="settings-drive" key={id}><label htmlFor={`drive-label-${id}`}>{drive?.role === 'harness-backups' ? 'Backup destination label' : 'Storage label'}<input id={`drive-label-${id}`} maxLength={120} value={label} onChange={event => onLabel(id, event.target.value)} aria-invalid={Boolean(errors[`driveLabels.${id}`])} aria-describedby={errors[`driveLabels.${id}`] ? `drive-error-${id}` : undefined}/></label>
        {errors[`driveLabels.${id}`] && <p className="settings-field-error" id={`drive-error-${id}`}>{errors[`driveLabels.${id}`]}</p>}
        <dl><dt>Logical identity</dt><dd>{id}</dd><dt>State</dt><dd>{drive ? `${drive.state}${drive.simulated?' · simulated':''}` : 'Unknown'}</dd><dt>Capacity</dt><dd>{bytes(drive?.capacityBytes)}</dd><dt>Available</dt><dd>{bytes(drive?.freeBytes)}</dd><dt>Temperature</dt><dd>{drive?.temperatureC == null ? 'Unknown' : `${drive.temperatureC} °C${drive.simulated?' · simulated':''}`}</dd><dt>Physical volume</dt><dd>{drive?.simulated?'Not connected':drive?.configured?'Configured in service':'Not configured'}</dd></dl>
      </div>;
    })}</div>
    <div className="settings-harness"><Archive size={18}/><div><strong>Memory Harness owns the schedule</strong><p>This app is the backup destination. Memory Harness generates and pushes its backups on its own schedule.</p><p>Last completed Harness transfer: <span>{lastHarnessTransfer ? new Date(lastHarnessTransfer).toLocaleString('en-GB', { timeZone: 'UTC' }) + (draft.simulated?' UTC · simulated':' UTC') : 'Unknown'}</span></p><small>A completed file transfer does not establish backup completeness or restorability. The file-storage disk has no configured backup.</small></div></div>
  </section>;
}
