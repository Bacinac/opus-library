import { TAKEN } from './status';
import type { Release } from './types';

/** Files decide ownership: a stale COMPLETE status must not keep a file-less
 *  ghost row looking owned. */
export const isOwned = (r: Release) => r.files_linked > 0 || TAKEN.includes(r.status);

// One pressing stands for each record and the rest step behind it, held or
// not — a remaster is not a second album and must not be a second line.
// Decided by the backend, because the player asks the same question of the
// same records and a rule kept in one client is a rule the other lacks.
//
// A pressing that does not stand for its record is never a line of its own,
// held or not: that test comes first, because holding both `Kill 'Em All`
// and `Kill 'Em All (Remastered)` is exactly the case that put one album on
// the shelf twice. What is left surfaces if it is held or tracked, whatever
// its category, and otherwise only if it is canonical.
export function isMain(r: Release): boolean {
	if (!r.stands) return false;
	if (isOwned(r)) return true;
	return r.canonical;
}

/** The artist's own shelf, by the record's own year and not the pressing's: a
 *  shelf ordered by reissue dates runs a band's history backwards. */
export const mainShelf = (releases: Release[]) =>
	releases.filter(isMain).toSorted((a, b) => (b.record_date ?? '').localeCompare(a.record_date ?? ''));

/** Some of it on disk and not all of it accounted for. */
export const partial = (release: Release) =>
	(release.status === 'none' && release.files_linked > 0) ||
	(release.status === 'complete' && release.unmatched_files > 0);

/** Two titles are one when only case, curly apostrophes or edge spaces differ. */
export function sameTitle(a: string, b: string): boolean {
	const canon = (s: string) => s.toLowerCase().replace(/[’‘]/g, "'").trim();
	return canon(a) === canon(b);
}
