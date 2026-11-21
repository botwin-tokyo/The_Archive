import { AlertTriangle, RotateCcw } from 'lucide-react';
import { Button } from './Button';

export function PanelFallback({ name = 'Panel', message, onRetry }: { name?: string; message?: string; onRetry?: () => void }) {
  return <div className="panel-fallback" role="alert"><AlertTriangle size={25}/><h3>{name} couldn’t load</h3>
    <p>{message ?? 'The other areas are still available. Try loading this panel again.'}</p>
    {onRetry && <Button icon={<RotateCcw size={15}/>} onClick={onRetry}>Retry panel</Button>}
  </div>;
}
