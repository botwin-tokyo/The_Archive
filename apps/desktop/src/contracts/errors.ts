import type { AppError, ErrorCode } from './models';
import { appErrorSchema } from './schemas';
export class ContractError extends Error {
  readonly detail: AppError;
  constructor(detail: AppError) { super(detail.message); this.name = 'ContractError'; this.detail = appErrorSchema.parse(detail); }
}
export function domainError(code: ErrorCode, message: string, retryable = false, correlationId = 'mock-contract', recovery: AppError['recovery'] = []): ContractError {
  return new ContractError({ code, message, retryable, correlationId, recovery });
}
export function safeError(error: unknown): AppError {
  if (error instanceof ContractError) return error.detail;
  return { code: 'INVALID_RESPONSE', message: 'The response could not be validated. Retry this panel.', retryable: true, correlationId: 'mock-validation', recovery: ['retry'] };
}
