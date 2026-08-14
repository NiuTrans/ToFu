import { type ContractRecord } from './contracts';
import { type DefinitionAdoptionResult } from './definition-adoption';

export interface WorkspaceSessionPort {
  currentId(): unknown;
  currentVersion(): number | null;
  documentToken(): unknown;
  applyDefinition(
    definition: unknown,
    id: unknown,
    options: { updatedAt: unknown },
  ): unknown;
  applyDefinitionResult(
    definition: unknown,
    id: unknown,
    options: { updatedAt: unknown },
  ): DefinitionAdoptionResult;
  acknowledgePersisted(id: unknown, version: unknown): unknown;
  detachPersisted(): unknown;
}

export interface WorkspaceMutationCoordinator {
  generation(id: unknown): number;
  advance(id: unknown): number;
  share<T>(kind: string, id: unknown, operation: () => T | PromiseLike<T>): Promise<T>;
}

export interface WorkspaceDefinitionRequests {
  canRead(): boolean;
  canSave(id: unknown): boolean;
  canRemove(): boolean;
  get(id: unknown): Promise<ContractRecord>;
  save(
    id: unknown,
    definition: unknown,
    expectedUpdatedAt: unknown,
  ): Promise<ContractRecord>;
  remove(id: unknown, expectedUpdatedAt: unknown): Promise<ContractRecord>;
}

export interface WorkspaceEditLifecycle {
  revision?(): unknown;
  requireValid?(action: string): unknown | PromiseLike<unknown>;
  createSaveCheckpoint(): unknown;
  isSaveCheckpointCurrent(checkpoint: unknown): boolean;
  completeSaveCheckpoint(
    checkpoint: unknown,
    inspection: unknown,
  ): unknown;
  setSaveBusy(value: boolean): void;
  markWriteConflict?(conflict: unknown): void;
  detachPersistedCheckpoint?(): void;
}

export interface WorkspacePersistenceContext {
  workspaceSession: WorkspaceSessionPort;
  mutations: WorkspaceMutationCoordinator;
  definitions: WorkspaceDefinitionRequests;
  lifecycle: WorkspaceEditLifecycle;
  has(name: string): boolean;
  call(name: string, ...args: readonly unknown[]): any;
  translate(key: string, params?: Record<string, unknown>): string;
  toast(message: string, error?: boolean): void;
  definitionsChanged(): void;
  normalizeInspection(value: unknown): unknown;
}
