import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Button, Dialog, Menu, Progress, Splitter } from './index';

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('Dialog', () => {
  function Fixture() {
    const [open, setOpen] = useState(false);
    return <><Button onClick={() => setOpen(true)}>Open dialog</Button><Dialog open={open} title="Rename entry" onClose={() => setOpen(false)}
      footer={<Button onClick={() => setOpen(false)}>Save name</Button>}><label>Name<input aria-label="Name"/></label><Button disabled>Unavailable action</Button></Dialog></>;
  }
  it('moves focus inside, contains forward and backward tabbing, escapes, and returns focus', () => {
    render(<Fixture/>);
    const opener = screen.getByRole('button', { name: 'Open dialog' });
    opener.focus();
    fireEvent.click(opener);
    expect(document.activeElement).toBe(screen.getByRole('textbox', { name: 'Name' }));
    const save = screen.getByRole('button', { name: 'Save name' });
    save.focus(); fireEvent.keyDown(save, { key: 'Tab' });
    const close = screen.getByRole('button', { name: 'Close dialog' });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(save);
    fireEvent.keyDown(save, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
  it('does not dismiss a non-dismissible submitting dialog', () => {
    const close = vi.fn();
    render(<Dialog open title="Submitting" dismissible={false} onClose={close}>Please wait</Dialog>);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(close).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Close dialog' })).toBeNull();
  });
});

describe('Splitter', () => {
  function Fixture() { const [value, setValue] = useState(310); return <Splitter orientation="vertical" label="Resize inspector" value={value} min={280} max={400} onChange={setValue} reverse/>; }
  it('supports bounded arrow, shift-arrow, Home, and End control', () => {
    render(<Fixture/>); const splitter = screen.getByRole('separator');
    fireEvent.keyDown(splitter, { key: 'ArrowLeft' }); expect(splitter.getAttribute('aria-valuenow')).toBe('320');
    fireEvent.keyDown(splitter, { key: 'ArrowLeft', shiftKey: true }); expect(splitter.getAttribute('aria-valuenow')).toBe('360');
    fireEvent.keyDown(splitter, { key: 'Home' }); expect(splitter.getAttribute('aria-valuenow')).toBe('280');
    fireEvent.keyDown(splitter, { key: 'ArrowRight' }); expect(splitter.getAttribute('aria-valuenow')).toBe('280');
    fireEvent.keyDown(splitter, { key: 'End' }); expect(splitter.getAttribute('aria-valuenow')).toBe('400');
  });
  it('handles pointer movement and stops resizing on release', () => {
    vi.stubGlobal('PointerEvent', MouseEvent);
    render(<Fixture/>); const splitter = screen.getByRole('separator');
    fireEvent.pointerDown(splitter, { button: 0, clientX: 500 });
    fireEvent.pointerMove(splitter, { clientX: 450 }); expect(splitter.getAttribute('aria-valuenow')).toBe('360');
    fireEvent.pointerMove(splitter, { clientX: 100 }); expect(splitter.getAttribute('aria-valuenow')).toBe('400');
    fireEvent.pointerUp(splitter);
    fireEvent.pointerMove(splitter, { clientX: 480 }); expect(splitter.getAttribute('aria-valuenow')).toBe('400');
  });
});

it('clamps known progress and keeps unknown totals indeterminate', () => {
  const { rerender } = render(<Progress value={120} label="Copy progress"/>);
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100');
  rerender(<Progress label="Copy progress"/>);
  expect(screen.getByRole('progressbar').hasAttribute('aria-valuenow')).toBe(false);
  expect(screen.getByRole('progressbar').getAttribute('aria-valuetext')).toBe('Calculating');
});

it('gives the menu keyboard navigation, disabled items, and focus restoration', () => {
  const selected = vi.fn();
  render(<Menu label="More" trigger="…" items={[{ id: 'a', label: 'First', onSelect: selected }, { id: 'b', label: 'Disabled', disabled: true, onSelect: selected }, { id: 'c', label: 'Last', onSelect: selected }]}/>);
  const opener = screen.getByRole('button', { name: 'More' });
  fireEvent.click(opener);
  expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'First' }));
  fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' });
  expect(document.activeElement).toBe(screen.getByRole('menuitem', { name: 'Last' }));
  fireEvent.click(document.activeElement!);
  expect(selected).toHaveBeenCalledTimes(1); expect(screen.queryByRole('menu')).toBeNull(); expect(document.activeElement).toBe(opener);
});
