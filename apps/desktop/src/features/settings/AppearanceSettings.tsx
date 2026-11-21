import { SlidersHorizontal } from 'lucide-react';
import type { Configuration } from '../../contracts';
export function AppearanceSettings({ value, onChange, simulated=true }: { simulated?:boolean;value: Configuration['appearance']; onChange: (value: Configuration['appearance']) => void }) {
  return <section className="settings-section" aria-labelledby="appearance-settings-title"><div className="settings-section-heading"><SlidersHorizontal size={19}/><div><h3 id="appearance-settings-title">Appearance</h3><p>{simulated?'Saved synthetic preferences for this session.':'Saved appearance preferences.'}</p></div></div>
    <label className="settings-check-row"><span>Reduced motion<small>Keep transitions understated</small></span><input type="checkbox" checked={value.reducedMotion} onChange={event => onChange({ ...value, reducedMotion: event.target.checked })}/></label>
    <label className="settings-check-row"><span>Compact rows<small>Use a denser file listing</small></span><input type="checkbox" checked={value.compactRows} onChange={event => onChange({ ...value, compactRows: event.target.checked })}/></label>
  </section>;
}
