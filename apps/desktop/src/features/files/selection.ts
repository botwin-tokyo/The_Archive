import type { Entry } from '../../contracts';

export const entryKey = (entry: Pick<Entry, 'driveId' | 'id'>) => `${entry.driveId}:${entry.id}`;
export const sameEntryRevision = (left: Entry, right: Entry) => entryKey(left) === entryKey(right) && left.revision === right.revision;

/** Selection is deliberately limited to the current page, never an unseen directory. */
export function reconcileSelection(selected: Entry[], entries: Entry[]): Entry[] {
  const current = new Map(entries.map((entry) => [entryKey(entry), entry]));
  return selected.flatMap((entry) => {
    const latest = current.get(entryKey(entry));
    return latest && sameEntryRevision(entry, latest) ? [latest] : [];
  });
}

export function selectRange(entries: Entry[], anchor: string | null, target: Entry): Entry[] {
  const targetIndex = entries.findIndex((entry) => entryKey(entry) === entryKey(target));
  const anchorIndex = entries.findIndex((entry) => entryKey(entry) === anchor);
  const start = anchorIndex < 0 ? targetIndex : anchorIndex;
  return entries.slice(Math.min(start, targetIndex), Math.max(start, targetIndex) + 1);
}
