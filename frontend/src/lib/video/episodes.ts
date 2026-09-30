import { t } from '$lib/i18n';
import { formatNumber } from '$lib/kit';
import { videoStateMark, type Count, type PageTag } from '$lib/opus';

export type EpisodeRow = {
	id: number;
	number: number;
	title: string;
	overview: string;
	air_date: string | null;
	still_url: string | null;
	runtime_min: number | null;
	/** whether anything is looking for it — a season watched and then deleted
	    is not a season with something missing from it */
	monitored: boolean;
	status: 'ignored' | 'wanted' | 'downloading' | 'complete' | 'waiting_subtitles';
	replacing: boolean;
	missing_subs: string[];
	file: { resolution: string; video_codec: string } | null;
};

/** The day as it is here, not in Greenwich: an episode that airs today is not
 *  a future one for the two hours the calendars disagree. */
export function localDay(now = new Date()): string {
	const pad = (n: number) => String(n).padStart(2, '0');
	return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Broadcast by `today`; an episode with no date yet is taken as out. */
export const aired = (e: Pick<EpisodeRow, 'air_date'>, today: string) => !(e.air_date && e.air_date > today);

export function countsOf(episodes: EpisodeRow[], today: string): Count[] {
	const upcoming = episodes.filter((e) => !aired(e, today)).length;
	return [
		{ icon: 'disk', n: formatNumber(episodes.filter((e) => !!e.file).length), text: t('series.tally.owned'), tone: 'ok' },
		...(upcoming ? [{ icon: 'clock', n: formatNumber(upcoming), text: t('series.tally.upcoming') }] : []),
		{ icon: 'list', n: formatNumber(episodes.length), text: t('series.tally.total') }
	];
}

export function marksOf(episode: EpisodeRow, future: boolean): PageTag[] {
	const state = future ? 'upcoming' : episode.status;
	const mark = videoStateMark(state);
	const marks: PageTag[] = episode.replacing
		? [{ ...videoStateMark('downloading')!, text: t('video.status.replacing') }]
		: [];
	if (mark)
		marks.push({
			...mark,
			text: future ? t('video.status.upcoming') : t(`video.status.${episode.status}`),
			title:
				state === 'waiting_subtitles'
					? t('video.status.missingSubs', { langs: episode.missing_subs.join(', ') })
					: ''
		});
	return marks;
}
