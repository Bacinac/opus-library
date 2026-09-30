import { describe, expect, test } from 'vitest';
import { t } from '$lib/i18n';
import { heldQuality, offeredQuality } from './quality';
import type { OfferedQuality, Quality } from './types';

const held = (q: Partial<Quality>): Quality => ({
	codec: 'flac',
	bitrate_kbps: null,
	sample_rate_hz: null,
	bit_depth: null,
	channels: 2,
	mixed: false,
	...q
});

const offered = (q: Partial<OfferedQuality>): OfferedQuality => ({
	codec: null,
	bit_depth: null,
	sample_rate_khz: null,
	bitrate_kbps: null,
	dsd: null,
	lossless: null,
	...q
});

describe('what is held', () => {
	test('lossless is its depth over its rate, in the reader’s own decimals', () => {
		expect(heldQuality(held({ bit_depth: 24, sample_rate_hz: 96000 }))).toBe('FLAC 24/96');
		expect(heldQuality(held({ bit_depth: 16, sample_rate_hz: 44100 }))).toBe('FLAC 16/44,1');
	});

	test('DSD is the multiple of the CD rate it runs at, whichever container holds it', () => {
		expect(heldQuality(held({ codec: 'dsf', sample_rate_hz: 2822400 }))).toBe('DSD64');
		expect(heldQuality(held({ codec: 'dff', sample_rate_hz: 5644800 }))).toBe('DSD128');
	});

	test('lossy is its bitrate, and what says nothing more is its codec', () => {
		expect(heldQuality(held({ codec: 'mp3', bitrate_kbps: 320 }))).toBe('MP3 320');
		expect(heldQuality(held({ codec: 'flac' }))).toBe('FLAC');
		expect(heldQuality(held({ codec: 'mixed' }))).toBe('Mixed');
	});

	test('nothing read is a dash, not a guess', () => {
		expect(heldQuality(null)).toBe('—');
	});
});

describe('what is offered', () => {
	test('reads like what is held', () => {
		expect(offeredQuality(offered({ codec: 'flac', bit_depth: 24, sample_rate_khz: 88.2 }))).toBe('FLAC 24/88,2');
		expect(offeredQuality(offered({ codec: 'mp3', bitrate_kbps: 256 }))).toBe('MP3 256');
	});

	test('DSD comes with its multiple already stated, or with none', () => {
		expect(offeredQuality(offered({ codec: 'dsd', dsd: 256 }))).toBe('DSD256');
		expect(offeredQuality(offered({ codec: 'dsd' }))).toBe('DSD');
	});

	test('an offer that names no codec says lossy only when it knows', () => {
		expect(offeredQuality(offered({ lossless: false }))).toBe(t('musicPicker.lossy'));
		expect(offeredQuality(offered({ lossless: true }))).toBe('—');
		expect(offeredQuality(offered({}))).toBe('—');
	});
});
