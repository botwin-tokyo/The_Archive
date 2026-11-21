import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { IconButton } from './IconButton';

export interface DialogProps {
  open: boolean;
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  className?: string;
  dismissible?: boolean;
}

const focusable = 'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';
const dialogStack: symbol[] = [];
let originalBodyOverflow = '';

export function Dialog({ open, title, description, onClose, children, footer, className = '', dismissible = true }: DialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const ref = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const returnTarget = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const token = Symbol('dialog');
    if (!dialogStack.length) originalBodyOverflow = document.body.style.overflow;
    dialogStack.push(token);
    document.body.style.overflow = 'hidden';
    const panel = ref.current;
    const first = panel?.querySelector<HTMLElement>('[autofocus], input:not(:disabled), select:not(:disabled), textarea:not(:disabled)')
      ?? panel?.querySelector<HTMLElement>(focusable);
    (first ?? panel)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (dialogStack.at(-1) !== token) return;
      if (event.key === 'Escape' && dismissible) {
        event.preventDefault();
        event.stopPropagation();
        closeRef.current();
      }
      if (event.key !== 'Tab' || !panel) return;
      const items = Array.from(panel.querySelectorAll<HTMLElement>(focusable))
        .filter(el => el.getAttribute('aria-hidden') !== 'true' && el.getAttribute('type') !== 'hidden' && !el.closest('[hidden]'))
        .sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
      if (!items.length) { event.preventDefault(); panel.focus(); return; }
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === firstItem || !panel.contains(document.activeElement))) {
        event.preventDefault(); lastItem.focus();
      } else if (!event.shiftKey && (document.activeElement === lastItem || !panel.contains(document.activeElement))) {
        event.preventDefault(); firstItem.focus();
      }
    };
    const onFocus = (event: FocusEvent) => {
      if (dialogStack.at(-1) !== token) return;
      if (panel && event.target instanceof Node && !panel.contains(event.target)) {
        (panel.querySelector<HTMLElement>(focusable) ?? panel).focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('focusin', onFocus);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('focusin', onFocus);
      const stackIndex = dialogStack.indexOf(token);
      if (stackIndex !== -1) dialogStack.splice(stackIndex, 1);
      if (!dialogStack.length) document.body.style.overflow = originalBodyOverflow;
      if (returnTarget?.isConnected) returnTarget.focus();
    };
  }, [open, dismissible]);

  if (!open) return null;
  return createPortal(<div className={`dialog-backdrop ${className.includes('drawer') ? 'dialog-backdrop--drawer' : ''}`}
    onMouseDown={event => { if (event.target === event.currentTarget && dismissible) onClose(); }}>
    <section ref={ref} role="dialog" aria-modal="true" aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined} tabIndex={-1} className={`dialog ${className}`}>
      <header className="dialog-header"><div><span className="eyebrow">The Archive</span><h2 id={titleId}>{title}</h2></div>
        {dismissible && <IconButton label="Close dialog" onClick={onClose}><X size={18}/></IconButton>}
      </header>
      {description && <p id={descriptionId} className="dialog-description">{description}</p>}
      <div className="dialog-body">{children}</div>
      {footer && <footer className="dialog-footer">{footer}</footer>}
    </section>
  </div>, document.body);
}
