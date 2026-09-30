// The shapes the music half of the backend serves. One declaration per response
// body, so a field that moves in the API breaks here and nowhere else. Unions
// are transcribed from the backend enums — a value the API cannot emit must not
// typecheck.
//
// What both halves share — a setting, a queue row, which media types this
// install has — is in $lib/core/types.

export type ReleaseStatus =
	| 'none'
	| 'wanted'
	| 'searching'
	| 'downloading'
	| 'complete'
	| 'failed';

export type ExternalLink = { source: string; url: string };

export type ArtistSummary = { id: number; name: string; image_url: string | null };

export type MusicStats = {
	artists: number;
	albums: number;
	tracks: number;
	complete: number;
	doubtful: number;
};

export type RelatedArtist = { id: number; name: string; monitored: boolean };

export type Quality = {
	codec: string;
	bitrate_kbps: number | null;
	sample_rate_hz: number | null;
	bit_depth: number | null;
	channels: number | null;
	mixed: boolean;
};

/** What a release on offer says it is, before any file of it is read. */
export type OfferedQuality = {
	codec: string | null;
	bit_depth: number | null;
	sample_rate_khz: number | null;
	bitrate_kbps: number | null;
	dsd: number | null;
	lossless: boolean | null;
};

// A mix of the album that exists somewhere else — Atmos, multichannel, the
// SACD layer. Noted when some other search named it; never downloaded.
export type ReleaseVariant = {
	variant: string;
	source: string;
	external_id: string | null;
	seen_at: string;
};

export type Release = {
	id: number;
	title: string;
	release_date: string | null;
	category: string;
	cover_url: string | null;
	status: ReleaseStatus;
	searching_channel: string | null;
	progress: number | null;
	quality: Quality | null;
	files_linked: number;
	/** which records of this music are on the shelf: "stereo", "5.1", "4.0" */
	editions: string[];
	unmatched_files: number;
	mixed_sources: boolean;
	variants: ReleaseVariant[];
	canonical: boolean;
	/** this pressing stands for the record — a remaster is not a second line */
	stands: boolean;
	/** what the record itself is called and dated, not this pressing of it */
	record_title: string;
	record_date: string | null;
	track_count: number | null;
	sources: string[];
};

export type ArtistDetail = {
	id: number;
	name: string;
	image_url: string | null;
	monitored: boolean;
	deezer_id: number | null;
	wikidata_id: string | null;
	artist_type: string | null;
	country: string | null;
	country_hr: string | null;
	begin_year: number | null;
	end_year: number | null;
	bio: string | null;
	enrich_status: string;
	links: ExternalLink[];
	members: RelatedArtist[];
	groups: RelatedArtist[];
	releases: Release[];
};

export type Candidate = { qid: string; label: string; description: string };

export type TrackRow = {
	id: number;
	position: number;
	title: string;
	duration_sec: number | null;
	has_file: boolean;
	file_id: number | null;
	file_name: string | null;
	tag_title: string | null;
	tag_artist: string | null;
	tag_album: string | null;
	tag_track: number | null;
	/** everything the file itself carries, as it carries it */
	tags: Record<string, string | string[]> | null;
};

export type ReleaseTracks = {
	description: string | null;
	status: ReleaseStatus;
	links: ExternalLink[];
	tracks: TrackRow[];
	unmatched_files: { id: number; name: string }[];
};

export type ArtistSearchResult = {
	deezer_id: number | null;
	spotify_id: string | null;
	name: string;
	image_url: string | null;
	nb_album: number | null;
	nb_fan: number | null;
	link: string | null;
	top_tracks: string[];
	matched_track: string | null;
};

export type ScanReason =
	| 'missing_tags'
	| 'files_unreadable'
	| 'artist_unresolved'
	| 'catalog_unreachable'
	| 'match_error'
	| 'cached_verdict'
	| 'already_complete'
	| 'no_catalog_candidate'
	| 'all_tracks_matched'
	| 'edition_confirmed'
	| 'partial_track_match';

export type ScanResult = {
	folder: string;
	outcome: string;
	artist?: string;
	artist_id?: number;
	album?: string;
	matched?: number;
	total?: number;
	/** A safe exception class for an administrator deciding whether to retry. */
	detail?: string;
	/** A controlled explanation of the matching decision, never an upstream response. */
	reason?: ScanReason;
};

export type ScanPhase = 'read' | 'artists' | 'match';

export type ScanState = {
	running: boolean;
	phase: ScanPhase | null;
	total: number;
	processed: number;
	matched: number;
	adopted_tracks: number;
	current: string | null;
	deep_total: number;
	deep_processed: number;
	deep_current: string | null;
	results: ScanResult[];
};

export type EnrichState = {
	running: boolean;
	total: number;
	processed: number;
	current: string | null;
	failed: string[];
};

export type UnmatchedFolder = {
	folder: string;
	files: number;
	tag_artist: string | null;
	tag_album: string | null;
};

export type ArtworkImage = {
	id: number;
	source: string;
	url: string;
	width: number | null;
	height: number | null;
	chosen: boolean;
	chosen_manual: boolean;
};
