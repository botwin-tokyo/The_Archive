import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  size?: 'small' | 'medium';
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'medium', icon, children, className = '', type = 'button', ...props }, ref,
) {
  return <button ref={ref} type={type} className={`btn btn--${variant} btn--${size} ${className}`} {...props}>
    {icon && <span className="button-icon" aria-hidden="true">{icon}</span>}{children}
  </button>;
});
