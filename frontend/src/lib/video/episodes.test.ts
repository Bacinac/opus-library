import { describe, expect, test } from 'vitest';
import { t } from '$lib/i18n';
import { aired, countsOf, localDay, marksOf, type EpisodeRow } from './episodes';

let next = 0;
const episode = (e: Partial<EpisodeRow>): EpisodeRow => ({
	id: ++next,
	number: next,
	title: `Episode ${next}`,
	overview: '',
	air_date: null,
	still_url: null,
	runtime_min: null,
	monitored: true,
	status: 'wanted',
	replacing: false,
	missing_subs: [],
	file: null,
	...e
});

const FILE = { resolution: '1080p', video_codec: 'h264' };

describe('the day', () => {
	test('is the date on the wall here, not in Greenwich', () => {
		expect(localDay(new Date(2026, 8, 3, 0, 30))).toBe('2026-09-03');
		expect(localDay(new Date(2026, 11, 31, 23, 59))).toBe('2026-12-31');
	});
});

describe('aired', () => {
	test('an episode is out on its own day, not the day after', () => {
		expect(aired(episode({ air_date: '2026-09-23' }), '2026-09-23')).toBe(true);
		expect(aired(episode({ air_date: '2026-09-24' }), '2026-09-23')).toBe(false);
	});

	test('an episode with no date is taken as out', () => {
		expect(aired(episode({}), '2026-09-23')).toBe(true);
	});
});

describe('the tally of a season', () => {
	test('names upcoming only when something is', () => {
		const season = [episode({ file: FILE }), episode({ air_date: '2026-01-01' })];
		expect(countsOf(season, '2026-09-23').map((c) => c.icon)).toEqual(['disk', 'list']);
		expect(countsOf([...season, episode({ air_date: '2026-10-01' })], '2026-09-23')).toEqual([
			expect.objectContaining({ icon: 'disk', n: '1' }),
			expect.objectContaining({ icon: 'clock', n: '1' }),
			expect.objectContaining({ icon: 'list', n: '3' })
		]);
	});
});

describe('the marks on an episode', () => {
	test('one that is simply on the shelf has none', () => {
		expect(marksOf(episode({ status: 'complete', file: FILE }), false)).toEqual([]);
	});

	test('one not broadcast yet is upcoming, whatever the library thinks of it', () => {
		const [mark] = marksOf(episode({ status: 'wanted' }), true);
		expect(mark.text).toBe(t('video.status.upcoming'));
	});

	test('a better copy on its way is said even of one already complete', () => {
		const marks = marksOf(episode({ status: 'complete', file: FILE, replacing: true }), false);
		expect(marks.map((m) => m.text)).toEqual([t('video.status.replacing')]);
	});

	test('waiting for subtitles names the languages it waits for', () => {
		const [mark] = marksOf(episode({ status: 'waiting_subtitles', file: FILE, missing_subs: ['hr', 'en'] }), false);
		expect(mark.title).toBe(t('video.status.missingSubs', { langs: 'hr, en' }));
	});
});
