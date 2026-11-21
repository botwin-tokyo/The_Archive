import type {SkillsPort} from './skills';
import type {VaultPort} from './vault';
import type {ImagePreview} from './imagePreview';
import type {GitBackupPort} from './gitBackup';
import type { VideoPort } from './video';
import type { AnimePort } from './anime';
import type { MusicPort } from './music';
import type { AgentSummary, Configuration, Diagnostic, DirectoryPage, DirectoryQuery, Drive, Entry, EntryRef, Job, JobControl, JobEvent, JobSnapshot, OperationReceipt, OperationRequest, ReadOptions, ServiceStatus, SyntheticSelection, TrashEntry, Unsubscribe } from './models';
export interface DrivePort { list(options?: ReadOptions): Promise<Drive[]>; }
export interface FileReadPort { previewImage?(ref: EntryRef, options?: ReadOptions): Promise<ImagePreview>; list(query: DirectoryQuery, options?: ReadOptions): Promise<DirectoryPage>; stat(ref: EntryRef, options?: ReadOptions): Promise<Entry>; }
export interface OperationPort { submit(request: OperationRequest): Promise<OperationReceipt>; reconcile(requestId: string): Promise<OperationReceipt | null>; }
export interface JobPort { snapshot(options?: ReadOptions): Promise<JobSnapshot>; subscribe(listener: (event: JobEvent) => void, onGap?: () => void): Unsubscribe; control(id: string, action: JobControl): Promise<Job>; }
export interface TrashPort { list(driveId: string, options?: ReadOptions): Promise<TrashEntry[]>; }
export interface ConfigurationPort { get(options?: ReadOptions): Promise<Configuration>; save(draft: Configuration): Promise<Configuration>; testHealth(): Promise<{ simulated: boolean; success: boolean; message: string }>; }
export interface AgentPort { list(options?: ReadOptions): Promise<AgentSummary[]>; updateScopes(id: string, revision: number, scopes: AgentSummary['scopes'], allowedRoots: AgentSummary['allowedRoots']): Promise<AgentSummary>; revoke(id: string, revision: number): Promise<AgentSummary>; }
export interface DiagnosticsPort { list(options?: ReadOptions): Promise<Diagnostic[]>; }
export interface DesktopSelectionPort { select(kind: SyntheticSelection['kind']): Promise<SyntheticSelection | null>; }
export interface StatusPort { get(options?: ReadOptions): Promise<ServiceStatus>; subscribe(listener: (status: ServiceStatus) => void): Unsubscribe; }
export interface BackendPorts { skills?: SkillsPort; vault?: VaultPort; gitBackup?: GitBackupPort; readonly mode: 'mock' | 'service'; music: MusicPort; video: VideoPort; anime: AnimePort; drives: DrivePort; files: FileReadPort; operations: OperationPort; jobs: JobPort; trash: TrashPort; configuration: ConfigurationPort; agents: AgentPort; diagnostics: DiagnosticsPort; selection: DesktopSelectionPort; status: StatusPort; }
