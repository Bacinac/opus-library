#!/usr/bin/env node
// The Library's words, checked by the package's checker against what this
// repository actually says: each family below names where its members come
// from, most of them the backend's own vocabularies. Run over the repository by
// ./check.sh.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { checkWords, quotedIn, report } from '../kit/words/check.mjs';

// This file sits at <root>/frontend/src/lib/i18n/.
const root = join(new URL('.', import.meta.url).pathname, '../../../..');
const read = (path) => readFileSync(join(root, path), 'utf8');
const from = (path, pattern) => quotedIn(root, path, pattern);
const all = (path, pattern) => [...read(path).matchAll(pattern)].map((m) => m[1]);

const SETTINGS = 'backend/opus/settings_store.py';
const specs = () =>
	[...read(SETTINGS).matchAll(/SettingSpec\(\s*"(\w+)",\s*"(\w+)"([^)]*)\)/g)].map((m) => ({
		key: m[1],
		group: m[2],
		rest: m[3]
	}));
// the token another module calls with belongs to the account page, not to a card
const shown = () => specs().filter((s) => s.group !== 'access');

const families = {
	field: { where: `the settings in ${SETTINGS}`, members: () => shown().map((s) => s.key) },
	'settings.group': {
		where: `the groups of ${SETTINGS}`,
		members: () => [...new Set(shown().map((s) => s.group))]
	},
	'settings.opt': {
		where: `the options of the select settings in ${SETTINGS}`,
		members: () =>
			shown().flatMap((s) => {
				if (!/kind="select"/.test(s.rest)) return [];
				const options = /options=tuple\(CEILINGS/.test(s.rest)
					? all(
							'backend/opus/music/matching/quality.py',
							/^CEILINGS[^=]*= \{([\s\S]*?)^\}/gm
						).flatMap((body) => [...body.matchAll(/"(\w+)":/g)].map((m) => m[1]))
					: [...(/options=\(([^)]*)/.exec(s.rest)?.[1] ?? '').matchAll(/"(\w+)"/g)].map((m) => m[1]);
				return options.map((o) => `${s.key}.${o}`);
			})
	},
	'settings.err': {
		where: `the refusals raised for a field in ${SETTINGS}`,
		members: () => all(SETTINGS, /SettingsValidationError\(spec\.key, "(\w+)"\)/g)
	},
	service: {
		where: `the built-in music channels, MUSIC_CHANNELS in ${SETTINGS}`,
		members: () => from(SETTINGS, /MUSIC_CHANNELS = \(([^*]*)/)
	},
	type: { where: 'MediaType in lib/core/types.ts', members: () => from('frontend/src/lib/core/types.ts', /MediaType = ([^;]*);/) },
	'music.status': {
		where: 'ReleaseStatus and MusicDownloadStatus in backend/opus/models/music.py, and JobStatus in backend/opus/acquire.py',
		members: () => [
			...['ReleaseStatus', 'MusicDownloadStatus'].flatMap((name) =>
				all('backend/opus/models/music.py', new RegExp(`class ${name}\\(enum\\.StrEnum\\):([\\s\\S]*?)\\n\\n\\n`, 'g'))
					.flatMap((body) => [...body.matchAll(/= "(\w+)"/g)].map((m) => m[1]))
			).filter((s) => s !== 'none'),
			...(/class JobStatus:[\s\S]*?state: str\s+# ([\w |]+)/.exec(read('backend/opus/acquire.py'))?.[1] ?? '')
				.split('|')
				.map((s) => s.trim())
		]
	},
	'libimport.outcome': {
		where: 'the outcomes listed in backend/opus/music/library/scan.py',
		members: () =>
			(/outcome: ([\w|#\s]+),/.exec(read('backend/opus/music/library/scan.py'))?.[1] ?? '')
				.split(/[|#\s]+/)
				.filter(Boolean)
	},
	category: {
		where: 'what backend/opus/music/classify.py returns',
		members: () => all('backend/opus/music/classify.py', /return "(\w+)"/g)
	},
	'artist.meta': {
		where: 'artist_type in backend/opus/models/music.py',
		members: () =>
			(/artist_type: .*# ([\w |]+)$/m.exec(read('backend/opus/models/music.py'))?.[1] ?? '')
				.split('|')
				.map((s) => s.trim())
	},
	'video.status': {
		// written out: the state is decided in three routers by conditionals rather
		// than listed anywhere
		where: 'the item states api/routers/video/shared.py, series.py and videos.py report',
		members: () => ['wanted', 'ignored', 'downloading', 'complete', 'waiting_subtitles']
	},
	'video.queue': {
		where: 'VideoDownload.state in backend/opus/models/video.py',
		members: () =>
			(/# (queued \| [\w |]+)$/m.exec(read('backend/opus/models/video.py'))?.[1] ?? '')
				.split('|')
				.map((s) => s.trim())
	},
	'video.library.outcome': {
		where: 'what backend/opus/video/library_scan.py records, less the outcomes that need no one',
		members: () =>
			all('backend/opus/video/library_scan.py', /_record\(path, "(\w+)"/g).filter(
				(o) => !['movie', 'episode', 'series', 'already_adopted'].includes(o)
			)
	},
	'video.library.phase': {
		where: 'the phases a running scan reports: starting, and those backend/opus/video/library_scan.py sets',
		members: () => ['starting', ...all('backend/opus/video/library_scan.py', /\["phase"\] = "(\w+)"/g)]
	},
	'detail.subsource': {
		where: 'Subtitle.source in backend/opus/models/video.py',
		members: () =>
			(/source: Mapped\[str\] = .*# ([\w |]+)$/m.exec(read('backend/opus/models/video.py'))?.[1] ?? '')
				.split('|')
				.map((s) => s.trim())
	}
};

report(checkWords({ root, packages: ['frontend/src/lib/kit', 'frontend/src/lib/opus'], families }));
