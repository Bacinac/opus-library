<script lang="ts">
	// The artist's identity card: portrait, the type/country/years line, the bio,
	// the people and the outbound links — plus the two actions that re-read the
	// artist from its sources. It draws what the page loaded and hands every
	// click back up.

	import { t, type MessageKey } from '$lib/i18n';
	import { Button, i18n } from '$lib/kit';
	import { MediaHead } from '$lib/opus';
	import type { ArtistDetail } from '$lib/music/types';

	let {
		artist,
		syncing,
		onpickportrait,
		onsync,
		onrefresh
	}: {
		artist: ArtistDetail;
		syncing: boolean;
		onpickportrait: () => void;
		onsync: () => void;
		onrefresh: () => void;
	} = $props();

	let meta = $derived(
		[
			artist.artist_type ? t(`artist.meta.${artist.artist_type}` as MessageKey) : '',
			(i18n.locale === 'hr' && artist.country_hr) || artist.country || '',
			artist.begin_year ? `${artist.begin_year}–${artist.end_year ?? ''}` : ''
		]
			.filter(Boolean)
			.join(' · ')
	);
</script>

<MediaHead
	title={artist.name}
	subtitle={meta}
	overview={artist.bio ?? ''}
	poster={artist.image_url}
	round
	onposter={onpickportrait}
	posterTitle={t('artwork.artistTitle')}
>
	{#snippet under()}
		{#if artist.members.length > 0}
			<div class="related">
				<span class="label">{t('artist.members')}</span>
				{#each artist.members as related (related.id)}
					<a href={`/artists/${related.id}`}>{related.name}</a>
				{/each}
			</div>
		{/if}
		{#if artist.groups.length > 0}
			<div class="related">
				<span class="label">{t('artist.memberOf')}</span>
				{#each artist.groups as related (related.id)}
					<a href={`/artists/${related.id}`}>{related.name}</a>
				{/each}
			</div>
		{/if}
		{#if artist.links.length > 0}
			<div class="related">
				<span class="label">{t('artist.links')}</span>
				{#each artist.links as link (link.url)}
					<a href={link.url} target="_blank" rel="noreferrer">{link.source}</a>
				{/each}
			</div>
		{/if}
	{/snippet}
	{#snippet actions()}
		<Button onclick={onsync} disabled={syncing}>{t('artist.syncDiscography')}</Button>
		<Button onclick={onrefresh}>{t('artist.refresh')}</Button>
	{/snippet}
</MediaHead>

<style>
	.related {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		column-gap: 0.9rem;
		row-gap: 0.15rem;
		margin-bottom: 0.4rem;
	}
	.label {
		font-size: var(--fs-xs);
		font-weight: 700;
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--muted);
		min-width: 5.5rem;
	}
	.related a {
		font-size: var(--fs-m);
		color: var(--muted);
		text-decoration: none;
	}
	.related a:hover {
		color: var(--accent);
		text-decoration: underline;
	}
</style>
