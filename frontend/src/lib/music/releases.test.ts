import { describe, expect, test } from 'vitest';
import { isMain, isOwned, mainShelf, partial, sameTitle } from './releases';
import type { Release } from './types';

let next = 0;
const release = (r: Partial<Release>) =>
	({
		id: ++next,
		title: `Release ${next}`,
		status: 'none',
		files_linked: 0,
		unmatched_files: 0,
		canonical: false,
		stands: true,
		record_date: null,
		...r
	}) as Release;

describe('owned', () => {
	test('is files on disk, or the shelf having taken it on', () => {
		expect(isOwned(release({ files_linked: 3 }))).toBe(true);
		expect(isOwned(release({ status: 'wanted' }))).toBe(true);
		expect(isOwned(release({ status: 'downloading' }))).toBe(true);
	});

	test('is not a complete status with nothing behind it', () => {
		expect(isOwned(release({ status: 'complete' }))).toBe(false);
	});
});

describe('the main shelf', () => {
	test('never holds a pressing that does not stand for its record, even one held', () => {
		expect(isMain(release({ stands: false, files_linked: 12, canonical: true }))).toBe(false);
	});

	test('holds what stands and is owned, canonical or not', () => {
		expect(isMain(release({ files_linked: 12 }))).toBe(true);
		expect(isMain(release({ status: 'wanted' }))).toBe(true);
	});

	test('holds what stands and is not owned only where it is canonical', () => {
		expect(isMain(release({ canonical: true }))).toBe(true);
		expect(isMain(release({}))).toBe(false);
	});

	test('runs newest record first by the record’s own date, the undated last', () => {
		const shelf = mainShelf([
			release({ title: 'Undated', canonical: true }),
			release({ title: 'Debut', canonical: true, record_date: '1973-03-01' }),
			release({ title: 'Remaster', stands: false, canonical: true, record_date: '2011-09-26' }),
			release({ title: 'Latest', canonical: true, record_date: '1979-11-30' })
		]);
		expect(shelf.map((r) => r.title)).toEqual(['Latest', 'Debut', 'Undated']);
	});
});

describe('partial', () => {
	test('is files for a release nobody asked for, or files a complete one cannot place', () => {
		expect(partial(release({ files_linked: 4 }))).toBe(true);
		expect(partial(release({ status: 'complete', files_linked: 10, unmatched_files: 1 }))).toBe(true);
	});

	test('is not a complete release with every file placed, nor an empty one', () => {
		expect(partial(release({ status: 'complete', files_linked: 10 }))).toBe(false);
		expect(partial(release({}))).toBe(false);
	});
});

describe('the same title', () => {
	test('ignores case, curly apostrophes and the spaces around it', () => {
		expect(sameTitle('Don’t Stop Me Now ', "don't stop me now")).toBe(true);
	});

	test('does not ignore what a reader would see', () => {
		expect(sameTitle('Time', 'Time (Remastered)')).toBe(false);
	});
});
