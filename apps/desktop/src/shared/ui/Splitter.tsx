import { useRef, type KeyboardEvent, type PointerEvent } from 'react';

export interface SplitterProps {
  orientation: 'horizontal' | 'vertical';
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
  label: string;
  reverse?: boolean;
  className?: string;
  controls?: string;
}

export function Splitter({ orientation, value, min, max, onChange, label, reverse = false, className = '', controls }: SplitterProps) {
  const start = useRef<{ coordinate: number; value: number } | null>(null);
  const horizontal = orientation === 'horizontal';
  const clamp = (next: number) => Math.min(max, Math.max(min, Math.round(next)));
  const point = (event: PointerEvent) => horizontal ? event.clientY : event.clientX;
  const onKeyDown = (event: KeyboardEvent) => {
    const negative = horizontal ? 'ArrowUp' : 'ArrowLeft';
    const positive = horizontal ? 'ArrowDown' : 'ArrowRight';
    const delta = event.shiftKey ? 40 : 10;
    if (![negative, positive, 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') onChange(min);
    else if (event.key === 'End') onChange(max);
    else onChange(clamp(value + (event.key === negative ? -delta : delta) * (reverse ? -1 : 1)));
  };
  return <div role="separator" tabIndex={0} aria-label={label} aria-orientation={orientation}
    aria-valuemin={min} aria-valuemax={max} aria-valuenow={value} aria-valuetext={`${value} pixels`}
    aria-controls={controls} className={`splitter splitter--${orientation} ${className}`} onKeyDown={onKeyDown}
    onPointerDown={event => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.currentTarget.focus();
      event.currentTarget.setPointerCapture?.(event.pointerId);
      start.current = { coordinate: point(event), value };
    }}
    onPointerMove={event => {
      if (start.current) onChange(clamp(start.current.value + (point(event) - start.current.coordinate) * (reverse ? -1 : 1)));
    }}
    onPointerUp={event => { start.current = null; if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); }}
    onPointerCancel={() => { start.current = null; }} onLostPointerCapture={() => { start.current = null; }}>
    <span aria-hidden="true"/>
  </div>;
}
