import type { Drive, Location } from '../../contracts';
export const driveStateLabel = (drive: Drive) => ({ online: 'Online', offline: 'Offline', 'read-only': 'Read only', full: 'Full', unconfigured: 'Not configured' })[drive.state];
export function usagePercent(drive: Drive): number | null {
  if (drive.capacityBytes === null || drive.freeBytes === null || BigInt(drive.capacityBytes) === 0n) return null;
  return Number((BigInt(drive.capacityBytes) - BigInt(drive.freeBytes)) * 100n / BigInt(drive.capacityBytes));
}
const STORAGE_KEY = 'archive-console:expanded-folders:v1';
export function readExpanded(fallback: Location): Set<string> {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null');
    if (Array.isArray(stored) && stored.length <= 64 && stored.every((item) => typeof item === 'string' && item.length < 5000)) return new Set(stored);
  } catch { /* A blocked preference store must never block browsing. */ }
  return new Set([JSON.stringify([fallback.driveId, ''])]);
}
export function saveExpanded(expanded: Set<string>) { try { localStorage.setItem(STORAGE_KEY, JSON.stringify([...expanded].slice(-64))); } catch { /* Session-only expansion is still usable. */ } }
