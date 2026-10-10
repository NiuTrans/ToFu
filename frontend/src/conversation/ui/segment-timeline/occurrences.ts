/** Settled timeline adapter to the shared producer-scoped occurrence matcher. */
import { createToolOccurrenceMatcher } from '../../presentation/tool-occurrences';
import type { TimelineRound } from './contracts';
export const createToolOccurrenceResolver = (rounds: readonly TimelineRound[]) => createToolOccurrenceMatcher(rounds);
