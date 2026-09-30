import type { ReleaseStatus } from './types';

/** A release something is already happening to: asking for it again would ask twice. */
export const BUSY: ReleaseStatus[] = ['searching', 'downloading'];

/** A release the shelf has taken on, whether or not a file has arrived yet. */
export const TAKEN: ReleaseStatus[] = ['wanted', ...BUSY];
