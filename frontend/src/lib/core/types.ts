// The shapes both halves of the library share. A shape only one half ever sees
// belongs to that half — $lib/music/types.ts, or the page that renders it.

/** The five vocabularies the library speaks. A type is present in the UI when
 * its library path is set, so an install with only music is a music app — with
 * no film word anywhere in it — without a second build. */
export type MediaType = 'music' | 'movies' | 'series' | 'video' | 'photos';

/** One row of the queue both halves answer. `state` stays in the vocabulary of
 * the half it came from: an album never waits for subtitles. */
export type QueueRow = {
	domain: 'music' | 'video';
	type: MediaType;
	id: number;
	wanted: string;
	release: string;
	channel: string;
	state: string;
	progress: number | null;
	detail: string;
	files: { name: string; state: string; progress: number }[];
	created_at: string;
};

/** An account with another service an installation added, linked through that
 * service's own approval page. */
export type Link = {
	name: string;
	pending: boolean;
	url: string | null;
	error: string | null;
	accounts: { id: number; label: string; created_at: string }[];
};
