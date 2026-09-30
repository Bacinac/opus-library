<script lang="ts">
	// One discography table, drawn the same way for the canonical releases and
	// for the demoted rest: cover, title, the quality and status readout, and
	// the actions that belong to a single release. The expanded row only hands
	// its cell to the tracklist the page renders into it.

	import type { Snippet } from 'svelte';
	import { t, type MessageKey } from '$lib/i18n';
	import { ArmedButton, Button, Progress, Tag, formatDate, formatNumber } from '$lib/kit';
	import { heldQuality } from '$lib/music/quality';
	import { partial } from '$lib/music/releases';
	import { BUSY } from '$lib/music/status';
	import type { Release, ReleaseTracks, ReleaseVariant } from '$lib/music/types';

	let {
		releases,
		expandedReleaseId,
		trackLists,
		tracklist,
		ontoggletracks,
		onpickcover,
		ondownload,
		ondeletefiles,
		onpick
	}: {
		releases: Release[];
		expandedReleaseId: number | null;
		trackLists: Record<number, ReleaseTracks>;
		tracklist: Snippet<[Release]>;
		ontoggletracks: (release: Release) => void;
		onpickcover: (release: Release) => void;
		ondownload: (release: Release, mode?: 'full' | 'replace' | 'surround' | 'dsd') => void;
		ondeletefiles: (release: Release) => void;
		onpick: (release: Release) => void;
	} = $props();

	/** The records of this music the shelf holds, said only where there is
	 *  something to say. Stereo is the album as everyone has it and needs no
	 *  name; anything else is the other edition standing beside it — or, where
	 *  there is no stereo master, the only one there is. */
	const otherEditions = (release: Release): string[] =>
		(release.editions ?? []).filter((edition) => edition !== 'stereo');

	const VARIANT_LABELS: Record<string, MessageKey> = {
		atmos: 'artist.variantAtmos',
		multichannel: 'artist.variantMultichannel',
		sacd: 'artist.variantSacd'
	};

	// One marker per mix however many sources named it: the table answers
	// whether the mix exists, and which source said so belongs in the tooltip.
	function variantMarkers(rows: ReleaseVariant[]) {
		const sources = new Map<string, string[]>();
		for (const row of rows) {
			const seen = sources.get(row.variant) ?? [];
			if (!seen.includes(row.source)) seen.push(row.source);
			sources.set(row.variant, seen);
		}
		return [...sources].map(([variant, from]) => ({
			variant,
			label: VARIANT_LABELS[variant] ? t(VARIANT_LABELS[variant]) : variant,
			title: t('artist.variantTitle', { sources: from.join(', ') })
		}));
	}
</script>

<div class="table-scroll">
	<table>
		<thead>
			<tr>
				<th></th>
				<th>{t('artist.col.release')}</th>
				<th>{t('artist.col.date')}</th>
				<th>{t('artist.col.type')}</th>
				<th>{t('artist.col.quality')}</th>
				<th>{t('artist.col.status')}</th>
				<th></th>
			</tr>
		</thead>
		<tbody>
			{#each releases as release (release.id)}
				{@const busy = BUSY.includes(release.status)}
				{@const date = release.record_date ?? release.release_date}
				<tr>
					<td>
						<button class="cover" onclick={() => onpickcover(release)} title={t('artwork.title')}
							aria-label={t('artwork.title')}>
							{#if release.cover_url}
								<img src={release.cover_url} alt={release.title} />
							{:else}
								<span class="blank"></span>
							{/if}
						</button>
					</td>
					<td class="wrap">
						<button class="title" onclick={() => ontoggletracks(release)}>
							<!-- the record's name where this pressing stands for it; its own
							     where it is one of the others, which is how they are told apart -->
							{release.stands ? release.record_title : release.title}
						</button>
					</td>
					<!-- the record's own year; the pressing's is the reissue date -->
					<td>{date ? formatDate(date) : '—'}</td>
					<td>{t(`category.${release.category}` as MessageKey)}</td>
					<td class="quality">
						{heldQuality(release.quality)}
						{#if release.quality?.channels === 1}
							<Tag title={t('artist.layoutTitle')}>{t('artist.layoutMono')}</Tag>
						{/if}
						{#each otherEditions(release) as edition (edition)}
							<Tag title={t('artist.layoutTitle')}>{edition}</Tag>
						{/each}
						{#each variantMarkers(release.variants) as marker (marker.variant)}
							<Tag tone="quiet" dashed title={marker.title}>{marker.label}</Tag>
						{/each}
						{#if release.mixed_sources || release.quality?.mixed}
							<Tag tone="err" title={t('artist.mixedSourcesTitle')}>{t('artist.mixedSources')}</Tag>
						{/if}
					</td>
					<td class={`status-${release.status}`}>
						{#if release.status === 'downloading'}
							<Progress inline share value={release.progress ?? 0} />
						{:else if release.status === 'searching' && release.searching_channel}
							{t('music.status.searching')} · {release.searching_channel}
						{:else if partial(release)}
							<span class="short">
								{t('music.status.partial', {
									linked: formatNumber(release.files_linked),
									total: formatNumber(
										Math.max(release.track_count ?? 0, release.files_linked + release.unmatched_files)
									)
								})}
							</span>
						{:else if release.status !== 'none'}
							{t(`music.status.${release.status}`)}
						{/if}
					</td>
					<td>
						<div class="actions">
							<!-- Another record of the same music, which is not a better copy
							     of this one and does not replace it. Each offered only where
							     that one particular edition is not already on the shelf — a
							     DSD rip already held is no reason to hide the surround button,
							     and the other way round. -->
							{#if release.status === 'complete' && !release.editions?.some((e) => e !== 'stereo' && e !== 'dsd')}
								<Button size="small" title={t('artist.surroundTitle')} onclick={() => ondownload(release, 'surround')}>
									{t('artist.surround')}
								</Button>
							{/if}
							{#if release.status === 'complete' && !release.editions?.includes('dsd')}
								<Button size="small" title={t('artist.dsdTitle')} onclick={() => ondownload(release, 'dsd')}>
									{t('artist.dsd')}
								</Button>
							{/if}
							{#if release.status === 'complete' && release.quality && release.quality.codec !== 'flac'}
								<Button size="small" tone="accent" title={t('artist.upgradeQualityTitle')}
									onclick={() => ondownload(release, 'full')}>
									{t('artist.upgradeQuality')}
								</Button>
							{:else if release.status !== 'complete'}
								{#if release.files_linked > 0}
									<Button size="small" tone="accent" onclick={() => ondownload(release, 'replace')} disabled={busy}>
										{t('artist.replaceIncomplete')}
									</Button>
									<Button size="small" title={t('artist.downloadFullTitle')}
										onclick={() => ondownload(release, 'full')} disabled={busy}>
										{t('artist.downloadFull')}
									</Button>
								{:else}
									<Button size="small" tone="accent" onclick={() => ondownload(release)} disabled={busy}>
										{t('action.download')}
									</Button>
								{/if}
							{/if}
							{#if release.files_linked > 0}
								<ArmedButton size="small" title={t('artist.deleteFilesTitle')} onconfirm={() => ondeletefiles(release)}>
									{t('artist.deleteFiles')}
								</ArmedButton>
							{/if}
							<!-- The automatic grab already had its turn on this album. When it
							     found nothing worth taking on its own — a label the catalog
							     names no artist for, a DSD edition it never guesses to look
							     for — a human can still see what is actually out there. -->
							<Button size="small" title={t('artist.pickTitle')} onclick={() => onpick(release)} disabled={busy}>
								{t('artist.pick')}
							</Button>
						</div>
					</td>
				</tr>
				{#if expandedReleaseId === release.id && trackLists[release.id]}
					<tr class="tracks-row">
						<td colspan="7">
							{@render tracklist(release)}
						</td>
					</tr>
				{/if}
			{/each}
		</tbody>
	</table>
</div>

<style>
	.cover {
		display: block;
		padding: 0;
		border: none;
		background: none;
		cursor: pointer;
	}
	.cover img,
	.blank {
		display: block;
		width: 44px;
		height: 44px;
		border-radius: 6px;
		object-fit: cover;
		background: var(--surface-2);
	}
	.quality {
		color: var(--muted);
		font-size: var(--fs-m);
	}
	.title {
		padding: 0;
		border: none;
		background: none;
		font: inherit;
		color: inherit;
		text-align: left;
		cursor: pointer;
	}
	.title:hover {
		color: var(--accent);
	}
	.tracks-row td {
		background: var(--surface);
		white-space: normal;
	}
	.actions {
		display: flex;
		gap: 0.35rem;
		justify-content: flex-end;
	}
	.short,
	.status-downloading,
	.status-searching {
		color: var(--warn);
	}
</style>
