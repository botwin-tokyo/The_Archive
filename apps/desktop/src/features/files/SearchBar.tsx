import { Search, X } from 'lucide-react';
import type { RefObject } from 'react';
interface Props { value: string; onChange: (value: string) => void; inputRef: RefObject<HTMLInputElement | null> }
export function SearchBar({ value, onChange, inputRef }: Props) {
  return <div className="files-search"><Search size={15} aria-hidden="true"/><input ref={inputRef} aria-label="Search names in this folder" placeholder="Search this folder" value={value} maxLength={255} onChange={(event) => onChange(event.target.value)}/>{value && <button aria-label="Clear search" onClick={() => onChange('')}><X size={14}/></button>}<span className="files-search-key">⌘ F</span></div>;
}
