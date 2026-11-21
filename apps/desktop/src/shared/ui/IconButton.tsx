import { forwardRef, type ButtonHTMLAttributes } from 'react';

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  active?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, children, className = '', active = false, type = 'button', ...props }, ref,
) {
  return <button ref={ref} type={type} aria-label={label} title={label}
    className={`icon-button ${active ? 'is-active' : ''} ${className}`} {...props}>{children}</button>;
});
