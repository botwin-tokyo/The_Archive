import type { Location } from '../../contracts';
export const sameLocation = (left: Location, right: Location) => left.driveId === right.driveId && left.path === right.path;
export const locationKey = (location: Location) => JSON.stringify([location.driveId, location.path]);
export function parentLocation(location: Location): Location {
  return { driveId: location.driveId, path: location.path.split('/').slice(0, -1).join('/') };
}
export interface BrowserHistory { locations: Location[]; index: number }
export function visit(history: BrowserHistory, location: Location): BrowserHistory {
  if (sameLocation(history.locations[history.index], location)) return history;
  const locations = [...history.locations.slice(0, history.index + 1), location].slice(-100);
  return { locations, index: locations.length - 1 };
}
