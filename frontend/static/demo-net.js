/* Synthetic API for the public OPUS demo.
 *
 * This file runs before SvelteKit. It intercepts only same-origin /api calls;
 * assets and navigation still use the real static site. State is cloned from a
 * deliberately fictional fixture and lives only in this browser tab.
 */
(function () {
	'use strict';

	// The public demo speaks English until the visitor picks a language.
	try {
		if (!localStorage.getItem('locale')) localStorage.setItem('locale', 'en');
	} catch {
		/* no storage */
	}

	const realFetch = window.fetch.bind(window);
	let fixture;
	let loading;
	let demoSettings;

	const clone = (value) => JSON.parse(JSON.stringify(value));
	const answer = (body, status = 200) =>
		new Response(body === undefined ? '' : JSON.stringify(body), {
			status,
			headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
		});

	async function data() {
		if (fixture) return fixture;
		loading ??= realFetch('/demo-fixtures.json', { cache: 'no-store' })
			.then((r) => {
				if (!r.ok) throw new Error(`demo fixtures: ${r.status}`);
				return r.json();
			})
			.then((d) => (fixture = clone(d)));
		return loading;
	}

	const setting = (key, group, value, kind = 'text', options = [], secret = false) => ({
		key, label: key, group, kind, options, secret,
		value: secret ? '' : value,
		is_set: secret ? Boolean(value) : null
	});

	function settings() {
		demoSettings ??= [
			setting('opus_url', 'acquire', 'http://opus-downloads:5173'),
			setting('opus_token', 'acquire', 'demo-token', 'text', [], true),
			setting('opus_landing_root', 'acquire', '/landing'),
			setting('opus_landing_dir', 'acquire', '/downloads'),
			setting('cleanup_after_import', 'acquire', 'true', 'bool'),
			setting('landing_keep_days', 'acquire', '14', 'number'),
			setting('music_dir', 'music_library', '/music'),
			setting('music_naming', 'music_library', '{artist}/{album} ({year})/{nn} - {title}'),
			setting('release_filter', 'music_library', 'albums', 'select', ['albums', 'albums_eps', 'all']),
			setting('music_quality_profile', 'music_acquire', 'prefer_lossless', 'select', ['prefer_lossless', 'lossless_only', 'any']),
			setting('max_quality', 'music_acquire', 'any', 'select', ['any', 'cd', '24_96', '24_192']),
			setting('min_candidate_score', 'music_acquire', '40', 'number'),
			setting('channel_order', 'music_acquire', 'slskd,sabnzbd', 'text', ['slskd', 'sabnzbd']),
			setting('spotify_client_id', 'music_metadata', 'demo-client'),
			setting('discogs_token', 'music_metadata', 'set', 'text', [], true),
			setting('movies_dir', 'video_library', '/movies'),
			setting('tv_dir', 'video_library', '/television'),
			setting('video_dir', 'video_library', '/video'),
			setting('protocol_preference', 'video_acquire', 'best_score', 'select', ['usenet_first', 'torrent_first', 'best_score']),
			setting('movie_quality_profile', 'video_acquire', '2160p', 'select', ['2160p', '1080p', '720p', 'any']),
			setting('tv_quality_profile', 'video_acquire', '1080p', 'select', ['2160p', '1080p', '720p', 'any']),
			setting('prefer_hdr', 'video_acquire', 'true', 'bool'),
			setting('prefer_surround', 'video_acquire', 'true', 'bool'),
			setting('streaming_subscriptions', 'video_acquire', '8,337', 'list'),
			setting('subtitle_langs', 'video_subtitles', 'hr,en'),
			setting('subtitle_mode', 'video_subtitles', 'all', 'select', ['any', 'all', 'none']),
			setting('accept_auto_subs', 'video_subtitles', 'false', 'bool'),
			setting('tmdb_api_key', 'video_metadata', 'set', 'text', [], true),
			setting('photos_dir', 'photos_library', '/photos'),
			setting('photos_timezone', 'photos_library', 'Europe/Zagreb'),
			setting('photos_derivatives_dir', 'photos_library', '/derivatives'),
			setting('photos_derive_workers', 'photos_library', '4', 'number'),
			setting('faces_threshold', 'photos_people', '0.50', 'number'),
			setting('faces_min_core', 'photos_people', '3', 'number'),
			setting('dida_url', 'photos_contacts', 'https://dida.example'),
			setting('dida_username', 'photos_contacts', 'opus')
		];
		return demoSettings;
	}

	const scanMusic = {
		running: false, phase: 'match', total: 18, processed: 18, matched: 16,
		adopted_tracks: 124, current: null, deep_total: 3, deep_processed: 3,
		deep_current: null,
		results: [
			{ folder: 'Demo Artist/Unknown Release', outcome: 'unmatched', artist: 'Demo Artist', album: 'Unknown Release', reason: 'no_catalog_candidate' },
			{ folder: 'Jadranski Kolektiv/Svjetla na rivi', outcome: 'adopted', artist: 'Jadranski Kolektiv', album: 'Svjetla na rivi', matched: 9, total: 9, reason: 'all_tracks_matched' }
		]
	};
	const enrich = { running: false, total: 3, processed: 3, current: null, failed: [] };
	const scanVideo = {
		running: false, phase: 'done', error: '', total: 14, processed: 14, movies: 2,
		series: 1, episodes: 6, unmatched: 1, green: 12, current: '',
		results: [{ file: 'Untitled 2026.mkv', path: '/movies/Untitled 2026.mkv', outcome: 'unmatched', guessed: 'Untitled' }],
		started_at: '2026-09-19T07:00:00Z', finished_at: '2026-09-19T07:02:12Z'
	};
	const releases = [{
		channel: 'usenet', title: 'Demo.2026.2160p.WEB-DL.DV.HDR.HEVC.Atmos', size: 17448304640,
		protocol: 'usenet', seeders: null, indexer: 'Demo Index', guid: 'demo-release', score: 94,
		resolution: '2160p', source: 'WEB-DL', video_codec: 'HEVC', subs_hint: true,
		audio_codec: 'Atmos', audio_channels: '7.1', hdr: 'DV HDR', mbps: 20.5, age_days: 2,
		payload: 17448304640, declared: 'video', ref: { demo: true }
	}];

	async function bodyOf(input, init) {
		const body = init?.body ?? (input instanceof Request ? await input.clone().text() : '');
		if (!body) return {};
		try { return typeof body === 'string' ? JSON.parse(body) : {}; } catch { return {}; }
	}

	async function api(path, method, url, input, init) {
		const d = await data();
		const body = await bodyOf(input, init);

		if (path === '/api/auth/session') return answer({ required: true, authenticated: true, username: 'demo', role: 'admin' });
		if (path === '/api/auth/logout' || path === '/api/auth/login') return answer({ ok: true });
		if (path === '/api/auth/token') return answer({ token: method === 'POST' ? 'opus_demo_new_token' : 'opus_demo_service_token' });
		if (path === '/api/auth/people' && method === 'GET') return answer({ people: [
			{ name: 'demo', display: 'Demo administrator', role: 'admin', disabled: false },
			{ name: 'family', display: 'Family member', role: 'user', disabled: false },
			{ name: 'gost', display: 'Guest', role: 'guest', disabled: false }
		] });
		if (path === '/api/auth/people' && method === 'POST') return answer({ name: body.name, display: body.display || body.name, role: body.role, disabled: false });
		if (path === '/api/auth/devices' && method === 'GET') return answer({ devices: [
			{ id: 1, name: 'Living room', module: 'player', collected: true, let_in_by: 'demo', let_in_at: '2026-09-01T18:00:00Z', seen_at: '2026-09-19T08:40:00Z' }
		] });
		if (path === '/api/auth/devices' && method === 'POST') return answer({ name: body.name || 'New device' });
		if (path === '/api/auth/password' || path.startsWith('/api/auth/people/') || path.startsWith('/api/auth/devices/')) return answer({ ok: true });

		if (path === '/api/version') return realFetch('/demo-version.json', { cache: 'no-store' });
		if (path === '/api/types') return answer(['music', 'movies', 'series', 'video', 'photos']);
		if (path === '/api/settings') {
			const list = settings();
			if (method === 'PUT') for (const s of list) if (body[s.key] !== undefined && !s.secret) s.value = String(body[s.key]);
			return answer(clone(list));
		}
		if (path === '/api/landing') return answer({ keep_days: 14, candidates: 5, protected: 2, stale: 2, reclaimable: 4831838208, entries: [
			{ path: 'film/demo-release', idle_days: 21, bytes: 3758096384 },
			{ path: 'music/old-candidate', idle_days: 18, bytes: 1073741824 }
		], truncated: false });
		if (path === '/api/links') return answer([]);
		if (path === '/api/words') return answer({ hr: {}, en: {} });
		if (path === '/api/video/discover/streaming') return answer([{ id: 8, name: 'Netflix' }, { id: 337, name: 'Disney+' }, { id: 119, name: 'Prime Video' }]);

		if (path === '/api/downloads') return answer(d.downloads);
		if (path === '/api/music/downloads/clear') { d.downloads = d.downloads.filter((x) => x.domain !== 'music' || ['queued', 'downloading', 'importing', 'downloaded'].includes(x.state)); return answer({ deleted: 1 }); }
		if (/^\/api\/(music|video)\/downloads\/\d+/.test(path)) {
			const id = Number(path.match(/\d+/)?.[0]); d.downloads = d.downloads.filter((x) => x.id !== id); return answer({ ok: true });
		}

		if (path === '/api/music/artists' && method === 'GET') return answer(d.artists);
		if (path === '/api/music/artists' && method === 'POST') return answer({ id: 99, name: 'New Demo Artist', releases: 4 });
		if (path === '/api/music/library/stats') return answer({ artists: 3, albums: 12, tracks: 124, complete: 10, doubtful: 2 });
		if (path === '/api/music/library/unmatched') return answer([{ folder: 'Demo Artist/Unknown Release', files: 7, tag_artist: 'Demo Artist', tag_album: 'Unknown Release' }]);
		if (path === '/api/music/library/scan') return answer(scanMusic);
		if (path === '/api/music/library/enrich') return answer(enrich);
		if (path === '/api/music/library/incomplete') return answer([{ release_id: 12, title: 'Između otoka', year: '2026', artist_id: 1, artist: 'Jadranski Kolektiv', have: 5, need: 8, origins: 1 }]);
		if (path === '/api/music/library/tags') return answer({ total: 2, keys: ['ARTIST', 'ALBUM', 'TITLE', 'TRACKNUMBER', 'DATE', 'GENRE'].map((key) => ({ key, files: 2 })), rows: [
			{ id: 1001, path: '/music/Jadranski Kolektiv/Svjetla na rivi/01 - Prvi trajekt.flac', artist: 'Jadranski Kolektiv', album: 'Svjetla na rivi', title: 'Prvi trajekt', track: 1, tags: { ARTIST: 'Jadranski Kolektiv', ALBUM: 'Svjetla na rivi', TITLE: 'Prvi trajekt', TRACKNUMBER: '1', DATE: '2024', GENRE: 'Electronic' } },
			{ id: 1002, path: '/music/Jadranski Kolektiv/Svjetla na rivi/02 - Svjetla na rivi.flac', artist: 'Jadranski Kolektiv', album: 'Svjetla na rivi', title: 'Svjetla na rivi', track: 2, tags: { ARTIST: 'Jadranski Kolektiv', ALBUM: 'Svjetla na rivi', TITLE: 'Svjetla na rivi', TRACKNUMBER: '2', DATE: '2024', GENRE: 'Electronic' } }
		] });
		if (path === '/api/music/search/artists') return answer([{ deezer_id: 999, spotify_id: null, name: 'Nova Obala', image_url: '/demo/artist.svg', nb_album: 6, nb_fan: 12840, link: null, top_tracks: ['Povratak', 'Valovi'], matched_track: null }]);
		let match = path.match(/^\/api\/music\/artists\/(\d+)$/);
		if (match) return answer(d.artist_details[match[1]] ?? d.artist_details['1']);
		match = path.match(/^\/api\/music\/releases\/(\d+)\/tracks$/);
		if (match) return answer(d.tracks[match[1]] ?? d.tracks['11']);
		if (/^\/api\/music\/(artists|releases)\/\d+\/artwork/.test(path)) return answer([{ id: 1, source: 'Demo catalogue', url: path.includes('/artists/') ? '/demo/artist.svg' : '/demo/album.svg', width: 1200, height: 1200, chosen: true, chosen_manual: false }]);
		if (/^\/api\/music\/releases\/\d+\/candidates$/.test(path)) return answer({ candidates: [{ channel: 'slskd', title: 'Jadranski Kolektiv - Demo [FLAC 24-96]', ref: { demo: true }, score: 92, completeness: 1, multi_album: false, whole_album: true, size: 1438814048, quality: { codec: 'flac', bit_depth: 24, sample_rate_khz: 96, bitrate_kbps: null, dsd: null, lossless: true } }] });
		if (path.startsWith('/api/music/')) return answer({ ok: true, files: 2, title: body.title ?? '', added: 1, linked: 1 });

		if (path === '/api/video/library/stats') return answer({ movies: { complete: 1, waiting: 1 }, series: { complete: 1, waiting: 1 }, movie_count: 3, series_count: 2, episodes: 14 });
		if (path === '/api/video/library/scan') return answer(scanVideo);
		if (path === '/api/video/movies' && method === 'GET') return answer(d.movies);
		if (path === '/api/video/series' && method === 'GET') return answer(d.series);
		if (path === '/api/video/videos' && method === 'GET') return answer({ channels: d.channels, videos: d.videos });
		if (path === '/api/video/search/movies') return answer([{ tmdb_id: 9001, title: 'Demo Horizon', original_title: 'Demo Horizon', year: 2026, poster_url: '/demo/poster.svg', streaming: [{ id: 8, name: 'Netflix', ours: true }] }]);
		if (path === '/api/video/search/series') return answer([{ tmdb_id: 9002, title: 'The Last Signal', original_title: 'The Last Signal', year: 2025, poster_url: '/demo/series.svg', streaming: [{ id: 119, name: 'Prime Video', ours: false }] }]);
		match = path.match(/^\/api\/video\/movies\/(\d+)$/);
		if (match && method === 'GET') return answer(d.movie_details[match[1]] ?? { ...d.movies.find((x) => x.id === Number(match[1])), files: [] });
		match = path.match(/^\/api\/video\/series\/(\d+)$/);
		if (match && method === 'GET') return answer(d.series_details[match[1]] ?? d.series_details['201']);
		if (/^\/api\/video\/(movies|episodes)\/\d+\/releases$/.test(path)) return answer(releases);
		if (/^\/api\/video\/(movies|episodes)\/\d+\/search$/.test(path)) return answer({ release: releases[0].title, queued: 1 });
		if (path === '/api/video/videos' && method === 'POST') return answer({ kind: 'video', title: 'Added demo video' });
		if (path.startsWith('/api/video/')) return answer({ ok: true, queued: 1 });

		if (path === '/api/photos/library/keeping-up') return answer({ doing: '', trouble: '' });
		if (path === '/api/photos/timeline/buckets') return answer({ months: [{ month: '2026-08', count: 2 }, { month: '2025-12', count: 1 }, { month: '2024-06', count: 1 }], total: 4, undated: 0 });
		if (path === '/api/photos/timeline') return answer({ photos: d.photos, next: null });
		if (path === '/api/photos/search') return answer({ terms: [
			{ kind: 'person', said: 'Ana', people: [{ id: 41, name: 'Ana Demo' }] },
			{ kind: 'place', said: 'zaljev', places: [{ place: 'Demo Bay', country: 'HR' }] }
		], unread: [] });
		if (path === '/api/photos/people') return answer(d.people);
		if (path === '/api/photos/people/suggested') return answer({ lives: d.lives, total: d.lives.length });
		if (path === '/api/photos/places') return answer(d.places);
		if (path === '/api/photos/clusters') return answer({ clusters: [{ id: 63, faces: 6, years: [2025, 2025], cover: 504 }, { id: 64, faces: 12, years: [2022, 2026], cover: 503 }] });
		match = path.match(/^\/api\/photos\/clusters\/(\d+)\/leanings$/);
		if (match) return answer({ leanings: [{ face: 503, name: 'Ana Demo' }], people: [{ person_id: 41, name: 'Ana Demo', faces: 1 }] });
		match = path.match(/^\/api\/photos\/clusters\/(\d+)$/);
		if (match && method === 'GET') return answer({ faces_total: 2, years: [2025, 2026], person: null, faces: [
			{ id: 503, score: 0.98, photo: 'demo-01', taken_at: '2026-08-17T18:42:00+02:00', person: null, fit: 0.91 },
			{ id: 504, score: 0.96, photo: 'demo-03', taken_at: '2025-12-24T20:05:00+01:00', person: null, fit: 0.82 }
		], next: null });
		match = path.match(/^\/api\/photos\/people\/(\d+)\/transformation$/);
		if (match) return answer({ frames: [{ year: 2024, face: 501, straightness: 0.9 }, { year: 2026, face: 503, straightness: 0.94 }], morph: { ms: 60, hold: 3, steps: 10 } });
		match = path.match(/^\/api\/photos\/(demo-\d+)$/);
		if (match && method === 'GET') return answer({ id: match[1], faces: [{ id: 501, x: 0.28, y: 0.18, w: 0.24, h: 0.3, person: { id: 41, name: 'Ana Demo' } }], place: 'Demo Bay', where: { lat: 43.5, lon: 16.4 } });
		if (path.startsWith('/api/photos/')) return answer({ ok: true, place: body.name ?? 'Demo Bay', photographs: body.day ? 4 : 1, person: { id: 41, name: body.name ?? 'Ana Demo' }, born_on: body.born_on ?? null, handed_back: [] });

		return answer({ detail: `Demo fixture is missing ${method} ${path}` }, 404);
	}

	window.fetch = async function (input, init) {
		const raw = input instanceof Request ? input.url : String(input);
		const url = new URL(raw, location.href);
		if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return realFetch(input, init);
		const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
		return api(url.pathname, method, url, input, init);
	};
})();
