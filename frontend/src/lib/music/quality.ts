import { t } from '$lib/i18n';
import { formatNumber } from '$lib/kit';
import type { OfferedQuality, Quality } from './types';

/** What the files of a release on the shelf are, as a collector reads it. */
export function heldQuality(q: Quality | null): string {
	if (!q) return '—';
	const codec = q.codec === 'mixed' ? 'Mixed' : q.codec.toUpperCase();
	if (q.codec === 'flac' && q.bit_depth && q.sample_rate_hz) {
		const khz = formatNumber(q.sample_rate_hz / 1000, { maximumFractionDigits: 1 });
		return `${codec} ${q.bit_depth}/${khz}`;
	}
	// DSD is 1 bit at a multiple of the CD rate, and states that multiple
	// rather than a depth/rate pair
	if ((q.codec === 'dsf' || q.codec === 'dff') && q.sample_rate_hz) {
		return `DSD${Math.round(q.sample_rate_hz / 44100)}`;
	}
	if (q.bitrate_kbps) return `${codec} ${formatNumber(q.bitrate_kbps)}`;
	return codec;
}

/** What a release on offer says it is, in the same words as what is held. */
export function offeredQuality(q: OfferedQuality): string {
	if (!q.codec) return q.lossless === false ? t('musicPicker.lossy') : '—';
	if (q.codec === 'dsd') return q.dsd ? `DSD${q.dsd}` : 'DSD';
	const codec = q.codec.toUpperCase();
	if (q.bit_depth && q.sample_rate_khz) {
		return `${codec} ${q.bit_depth}/${formatNumber(q.sample_rate_khz, { maximumFractionDigits: 1 })}`;
	}
	if (q.bitrate_kbps) return `${codec} ${formatNumber(q.bitrate_kbps)}`;
	return codec;
}
