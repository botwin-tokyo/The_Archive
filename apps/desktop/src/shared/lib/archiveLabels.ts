/** Display-only acquisition suffix cleanup. Never use these labels as file references. */
export function archiveNameLabel(name: string): string {
  return name.replace(/ \[video-[a-f0-9]{12}\](?=\.(?:mp4|mp3)$)/, '').replace(/ \[(?:[A-Za-z0-9]{22}|[a-f0-9]{16})\](?=\.(?:mp3|lrc|txt)$|$)/, '');
}
export function archivePathLabel(path: string): string {
  return path.split('/').map(archiveNameLabel).join('/');
}
